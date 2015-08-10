# -*- coding: utf-8 -*-

# Copyright 2008-2015 Jaap Karssenberg <jaap.karssenberg@gmail.com>


from __future__ import with_statement

import os
import re
import weakref
import logging
import threading

logger = logging.getLogger('zim.notebook')

import zim.templates
import zim.formats

from zim.fs import File, Dir
from zim.errors import Error, TrashNotSupportedError
from zim.config import HierarchicDict
from zim.parsing import is_interwiki_keyword_re, link_type, is_win32_path_re
from zim.signals import ConnectorMixin, SignalEmitter, SIGNAL_NORMAL

from .page import Path, Page, StoreNodePage, HRef, HREF_REL_ABSOLUTE, HREF_REL_FLOATING
from .index import IndexNotFoundError, LINK_DIR_BACKWARD

DATA_FORMAT_VERSION = (0, 4)


from zim.config import INIConfigFile, String, ConfigDefinitionByClass, Boolean, Choice


class IndexNotUptodateError(Error):
	pass # TODO description here?


class NotebookConfig(INIConfigFile):
	'''Wrapper for the X{notebook.zim} file'''

	# TODO - unify this call with NotebookInfo ?

	def __init__(self, file):
		INIConfigFile.__init__(self, file)
		if os.name == 'nt': endofline = 'dos'
		else: endofline = 'unix'
		self['Notebook'].define((
			('version', String('.'.join(map(str, DATA_FORMAT_VERSION)))),
			('name', String(file.dir.basename)),
			('interwiki', String(None)),
			('home', ConfigDefinitionByClass(Path('Home'))),
			('icon', String(None)), # XXX should be file, but resolves relative
			('document_root', String(None)), # XXX should be dir, but resolves relative
			('shared', Boolean(True)),
			('endofline', Choice(endofline, set(('dos', 'unix')))),
			('disable_trash', Boolean(False)),
			('profile', String(None)),
		))


def _resolve_relative_config(dir, config):
	# Some code shared between Notebook and NotebookInfo

	# Resolve icon, can be relative
	icon = config.get('icon')
	if icon:
		if zim.fs.isabs(icon) or not dir:
			icon = File(icon)
		else:
			icon = dir.resolve_file(icon)

	# Resolve document_root, can also be relative
	document_root = config.get('document_root')
	if document_root:
		if zim.fs.isabs(document_root) or not dir:
			document_root = Dir(document_root)
		else:
			document_root = dir.resolve_dir(document_root)

	return icon, document_root


def _iswritable(dir):
	if os.name == 'nt':
		# Test access - (iswritable turns out to be unreliable
		# for folders on windows..)
		f = dir.file('.zim.tmp')
		try:
			f.write('Test')
			f.remove()
		except:
			return False
		else:
			return True
	else:
		return dir.iswritable()


def _cache_dir_for_dir(dir):
	# Consider using md5 for path name here, like thumbnail spec
	from zim.config import XDG_CACHE_HOME

	if os.name == 'nt':
		path = 'notebook-' + dir.path.replace('\\', '_').replace(':', '').strip('_')
	else:
		path = 'notebook-' + dir.path.replace('/', '_').strip('_')

	return XDG_CACHE_HOME.subdir(('zim', path))


class Notebook(ConnectorMixin, SignalEmitter):
	'''Main class to access a notebook

	This class defines an API that proxies between backend L{zim.stores}
	and L{Index} objects on the one hand and the user interface on the
	other hand. (See L{module docs<zim.notebook>} for more explanation.)

	@signal: C{store-page (page)}: emitted before actually storing the page
	@signal: C{stored-page (page)}: emitted after storing the page
	@signal: C{move-page (oldpath, newpath, update_links)}: emitted before
	actually moving a page
	@signal: C{moved-page (oldpath, newpath, update_links)}: emitted after
	moving the page
	@signal: C{delete-page (path)}: emitted before deleting a page
	@signal: C{deleted-page (path)}: emitted after deleting a page
	means that the preferences need to be loaded again as well
	@signal: C{properties-changed ()}: emitted when properties changed
	@signal: C{suggest-link (path, text)}: hook that is called when trying
	to resolve links
	@signal: C{new-page-template (path, template)}: emitted before
	evaluating a template for a new page, intended for plugins that want
	to extend page templates

	@note: For store_async() the 'page-stored' signal is emitted
	after scheduling the store, but potentially before it was really
	executed. This may bite when you do direct access to the underlying
	files - however when using the API this should not be visible.

	@ivar name: The name of the notebook (string)
	@ivar icon: The path for the notebook icon (if any)
	# FIXME should be L{File} object
	@ivar document_root: The L{Dir} object for the X{document root} (if any)
	@ivar dir: Optional L{Dir} object for the X{notebook folder}
	@ivar file: Optional L{File} object for the X{notebook file}
	@ivar cache_dir: A L{Dir} object for the folder used to cache notebook state
	@ivar config: A L{SectionedConfigDict} for the notebook config
	(the C{X{notebook.zim}} config file in the notebook folder)
	@ivar lock: An C{threading.Lock} for async notebook operations
	@ivar profile: The name of the profile used by the notebook or C{None}

	In general this lock is not needed when only reading data from
	the notebook. However it should be used when doing operations that
	need a fixed state, e.g. exporting the notebook or when executing
	version control commands on the storage directory.

	@ivar index: The L{Index} object used by the notebook
	'''

	# Signals for store, move and delete are defined double with one
	# emitted before the action and the other after the action run
	# successfully. This is different from the normal connect vs.
	# connect_after strategy. However in exceptions thrown from
	# a signal handler are difficult to handle, so we split the signal
	# in two steps.

	# TODO add checks for read-only page in much more methods

	# define signals we want to use - (closure type, return type and arg types)
	__gsignals__ = {
		'store-page': (SIGNAL_NORMAL, None, (object,)),
		'stored-page': (SIGNAL_NORMAL, None, (object,)),
		'move-page': (SIGNAL_NORMAL, None, (object, object, bool)),
		'moved-page': (SIGNAL_NORMAL, None, (object, object, bool)),
		'delete-page': (SIGNAL_NORMAL, None, (object,)),
		'deleted-page': (SIGNAL_NORMAL, None, (object,)),
		'properties-changed': (SIGNAL_NORMAL, None, ()),
		'suggest-link': (SIGNAL_NORMAL, None, (object, object)),
		'new-page-template': (SIGNAL_NORMAL, None, (object, object)),
	}
	__hooks__ = ('suggest-link',)

	properties = (
		('name', 'string', _('Name')), # T: label for properties dialog
		('interwiki', 'string', _('Interwiki Keyword'), lambda v: not v or is_interwiki_keyword_re.search(v)), # T: label for properties dialog
		('home', 'page', _('Home Page')), # T: label for properties dialog
		('icon', 'image', _('Icon')), # T: label for properties dialog
		('document_root', 'dir', _('Document Root')), # T: label for properties dialog
		#~ ('profile', 'string', _('Profile'), list_profiles), # T: label for properties dialog
		('profile', 'string', _('Profile')), # T: label for properties dialog
		# 'shared' property is not shown in properties anymore
	)

	@classmethod
	def new_from_dir(klass, dir):
		assert isinstance(dir, Dir)

		from .stores.files import FilesStore
		from .index import Index

		config = NotebookConfig(dir.file('notebook.zim'))
		endofline = config['Notebook']['endofline']
		shared = config['Notebook']['shared']

		subdir = dir.subdir('.zim')
		if not shared and subdir.exists() and _iswritable(subdir):
			cache_dir = subdir
		else:
			cache_dir = _cache_dir_for_dir(dir)

		store = FilesStore(dir, endofline)
		index = Index.new_from_file(cache_dir.file('index.db'), store)

		return klass(dir, cache_dir, config, store, index)

	def __init__(self, dir, cache_dir, config, store, index):
		self.dir = dir
		self.cache_dir = cache_dir
		self.config = config
		self.store = store
		self.index = index

		self.readonly = not _iswritable(dir) if dir else None # XXX

		if self.readonly:
			logger.info('Notebook read-only: %s', dir.path)

		self.namespace_properties = HierarchicDict({
				'template': 'Default'
			})
		self._page_cache = weakref.WeakValueDictionary()

		self.name = None
		self.icon = None
		self.document_root = None

		self.lock = threading.Lock()
			# We don't use FS.get_async_lock() at this level. A store
			# backend will automatically trigger this when it calls any
			# async file operations. This one is more abstract for the
			# notebook as a whole, regardless of storage

		from .index import PagesView, LinksView, TagsView
		self.pages = PagesView.new_from_index(self.index)
		self.links = LinksView.new_from_index(self.index)
		self.tags = TagsView.new_from_index(self.index)

		def on_page_updated(index, indexpath):
			## FIXME still not called for parent pages -- need refactor
			## of index to deal with this properly I'm afraid...
			#~ print "UPDATED", indexpath
			if indexpath.name in self._page_cache:
				#~ print "  --> IN CAHCE"
				self._page_cache[indexpath.name].haschildren = indexpath.haschildren

		self.index.connect('page-added', on_page_updated)
		self.index.connect('page-changed', on_page_updated)

		#~ if self.needs_upgrade:
			#~ logger.warn('This notebook needs to be upgraded to the latest data format')

		self.do_properties_changed()

	@property
	def uri(self):
		'''Returns a file:// uri for this notebook that can be opened by zim'''
		return self.dir.uri

	@property
	def info(self):
		'''The L{NotebookInfo} object for this notebook'''
		try:
			uri = self.uri
		except AssertionError:
			uri = None

		return NotebookInfo(uri, **self.config['Notebook'])

	@property
	def profile(self):
		'''The 'profile' property for this notebook'''
		return self.config['Notebook'].get('profile') or None # avoid returning ''

	def save_properties(self, **properties):
		'''Save a set of properties in the notebook config

		This method does an C{update()} on the dict with properties but
		also updates the object attributes that map those properties.

		@param properties: the properties to update

		@emits: properties-changed
		'''
		# Check if icon is relative
		icon = properties.get('icon')
		if icon and not isinstance(icon, basestring):
			assert isinstance(icon, File)
			if self.dir and icon.ischild(self.dir):
				properties['icon'] = './' + icon.relpath(self.dir)
			else:
				properties['icon'] = icon.user_path or icon.path

		# Check document root is relative
		root = properties.get('document_root')
		if root and not isinstance(root, basestring):
			assert isinstance(root, Dir)
			if self.dir and root.ischild(self.dir):
				properties['document_root'] = './' + root.relpath(self.dir)
			else:
				properties['document_root'] = root.user_path or root.path

		# Set home page as string
		if 'home' in properties and isinstance(properties['home'], Path):
			properties['home'] = properties['home'].name

		# Actual update and signals
		# ( write is the last action - in case update triggers a crash
		#   we don't want to get stuck with a bad config )
		self.config['Notebook'].update(properties)
		self.emit('properties-changed')

		if hasattr(self.config, 'write'): # Check needed for tests
			self.config.write()

	def do_properties_changed(self):
		config = self.config['Notebook']

		self.name = config['name']
		icon, document_root = _resolve_relative_config(self.dir, config)
		if icon:
			self.icon = icon.path # FIXME rewrite to use File object
		else:
			self.icon = None
		self.document_root = document_root

		# TODO - can we switch cache_dir on run time when 'shared' changed ?

	def lookup_pagename_from_user_input(self, name, reference=None):
		'''Lookup a pagename based on user input
		@param name: the user input as string
		@param reference: a L{Path} in case reletive links are supported as
		customer input
		@returns: a L{IndexPath} or L{Path} for C{name}
		@raises ValueError: when C{name} would reduce to empty string
		after removing all invalid characters, or if C{name} is a
		relative link while no C{reference} page is given.
		@raises IndexNotFoundError: when C{reference} is not indexed
		'''
		return self.pages.lookup_from_user_input(name, reference)

	def relative_link(self, source, href):
		'''Returns a relative links for a page link

		More or less the opposite of resolve_link().

		@param source: L{Path} for the referring page
		@param href: L{Path} for the linked page
		@returns: a link for href, either relative to 'source' or an
		absolute link
		'''
		if href == source: # page linking to itself
			return href.basename
		elif href.ischild(source): # link to a child or grand child
			return '+' + href.relname(source)
		else:
			parent = source.commonparent(href)
			if parent.isroot: # no common parent except for root
				if href.parts[0].lower() in [p.lower() for p in source.parts]:
					# there is a conflicting anchor name in path
					return ':' + href.name
				else:
					return href.name
			elif parent == href: # link to an parent or grand parent
				return href.basename
			elif parent == source.parent: # link to sibling of same parent
				return href.relname(parent)
			else:
				return parent.basename + ':' + href.relname(parent)

	def suggest_link(self, source, word):
		'''Suggest a link Path for 'word' or return None if no suggestion is
		found. By default we do not do any suggestion but plugins can
		register handlers to add suggestions using the 'C{suggest-link}'
		signal.
		'''
		return self.emit('suggest-link', source, word)

	def get_page(self, path):
		'''Get a L{Page} object for a given path

		This method requests the page object from the store object and
		hashes it in a weakref dictionary to ensure that an unique
		object is being used for each page.

		Typically a Page object will be returned even when the page
		does not exist. In this case the C{hascontent} attribute of
		the Page will be C{False} and C{get_parsetree()} will return
		C{None}. This means that you do not have to create a page
		explicitly, just get the Page object and store it with new
		content (if it is not read-only of course).

		However in some cases this method will return C{None}. This
		means that not only does the page not exist, but also that it
		can not be created. This should only occur for certain special
		pages and depends on the store implementation.

		@param path: a L{Path} object
		@returns: a L{Page} object or C{None}
		'''
		# As a special case, using an invalid page as the argument should
		# return a valid page object.
		assert isinstance(path, Path)
		if path.name in self._page_cache \
		and self._page_cache[path.name].valid:
			return self._page_cache[path.name]
		else:
			node = self.store.get_node(path)
			page = StoreNodePage(path, node)
			try:
				indexpath = self.pages.lookup_by_pagename(path)
			except IndexNotFoundError:
				pass
				# TODO trigger indexer here if page exists !
			else:
				if indexpath and indexpath.haschildren:
					page.haschildren = True
				# page might be the parent of a placeholder, in that case
				# the index knows it has children, but the store does not

			# TODO - set haschildren if page maps to a store namespace
			self._page_cache[path.name] = page
			return page

	def get_new_page(self, path):
		'''Like get_page() but guarantees the page does not yet exist
		by adding a number to the name to make it unique.

		This method is intended for cases where e.g. a automatic script
		wants to store a new page without user interaction. Conflicts
		are resolved automatically by appending a number to the name
		if the page already exists. Be aware that the resulting Page
		object may not match the given Path object because of this.

		@param path: a L{Path} object
		@returns: a L{Page} object
		'''
		i = 0
		base = path.name
		page = self.get_page(path)
		while page.hascontent or page.haschildren:
			i += 1
			path = Path(base + ' %i' % i)
			page = self.get_page(path)
		return page

	def flush_page_cache(self, path):
		'''Flush the cache used by L{get_page()}

		After this method calling L{get_page()} for C{path} or any of
		its children will return a fresh page object. Be aware that the
		old Page objects may still be around but will be flagged as
		invalid and can no longer be used in the API.

		@param path: a L{Path} object
		'''
		names = [path.name]
		ns = path.name + ':'
		names.extend(k for k in self._page_cache.keys() if k.startswith(ns))
		for name in names:
			if name in self._page_cache:
				page = self._page_cache[name]
				assert not page.modified, 'BUG: Flushing page with unsaved changes'
				page.valid = False
				del self._page_cache[name]

	def get_home_page(self):
		'''Returns a L{Page} object for the home page'''
		return self.get_page(self.config['Notebook']['home'])

	def store_page(self, page):
		'''Save the data from the page in the storage backend

		@param page: a L{Page} object
		@emits: store-page before storing the page
		@emits: stored-page on success
		'''
		assert page.valid, 'BUG: page object no longer valid'
		self.emit('store-page', page)
		page._store()
		self.index.on_store_page(page)
		self.emit('stored-page', page)

	def store_page_async(self, page):
		'''Save the data from a page in the storage backend
		asynchronously

		Like L{store_page()} but asynchronous, so the method returns
		as soon as possible without waiting for success. Falls back to
		L{store_page()} when the backend does not support asynchronous
		operations.

		@param page: a L{Page} object
		@returns: A L{FunctionThread} for the background job or C{None}
		if save was performed in the foreground
		@emits: store-page before storing the page
		@emits: stored-page on success
		'''
		assert page.valid, 'BUG: page object no longer valid'
		self.emit('store-page', page)
		func = self.store.store_page_async(page)
		try:
			self.emit('stored-page', page)
				# FIXME - stored-page is emitted early, but emitting from
				# the thread is also not perfect, since the page may have
				# changed already in the gui
				# (we tried this and it broke autosave for some users!)
		finally:
			return func

	def move_page(self, path, newpath, update_links=True, callback=None):
		'''Move a page in the notebook

		@param path: a L{Path} object for the old/current page name
		@param newpath: a L{Path} object for the new page name
		@param update_links: if C{True} all links B{from} and B{to} this
		page and any of it's children will be updated to reflect the
		new page name

		The original page C{path} does not have to exist, in this case
		only the link update will done. This is useful to update links
		for a placeholder.

		@param callback: a callback function which is called for each
		page that is updates when updating links. It is called as::

			callback(page, total=None)

		Where:
		  - C{page} is the L{Page} object for the page being updated
		  - C{total} is an optional parameter for the number of pages
		    still to go - if known

		@raises PageExistsError: if C{newpath} already exists

		@emits: move-page before the move
		@emits: moved-page after succesful move
		'''
		logger.debug('Move page %s to %s', path, newpath)
		assert callback is None # TODO TODO - iterator version
		if update_links and not self.index.probably_uptodate:
			raise IndexNotUptodateError, 'Index not up to date'

		n_links = self.links.n_list_links_section(path, LINK_DIR_BACKWARD)
		self.store.move_page(path, newpath)
		if not (newpath == path or newpath.ischild(path)):
			self.index.on_delete_page(path)
		self.index.update(newpath) # TODO - optimize by letting indexers know about move
		self.flush_page_cache(path)
		if not update_links:
			return

		self._update_links_in_moved_page(path, newpath)
		self._update_links_to_moved_page(path, newpath)
		new_n_links = self.links.n_list_links_section(newpath, LINK_DIR_BACKWARD)
		if new_n_links != n_links:
			logger.warn('Number of links after move (%i) does not match number before move (%i)', new_n_links, n_links)
		else:
			logger.debug('Number of links after move does match number before move (%i)', new_n_links)

	def _update_links_in_moved_page(self, oldtarget, newtarget):
		# Find (floating) links that originate from the moved page
		# check if they would resolve different from the old location
		seen = set()
		for link in list(self.links.list_links_section(newtarget)):
			if link.source.name not in seen \
			and not (
				link.target == newtarget
				or link.target.ischild(newtarget)
			):
				if link.source == newtarget:
					oldpath = oldtarget
				else:
					oldpath = oldtarget + link.source.relname(newtarget)
				self._update_moved_page(link.source, oldpath)

	def _update_moved_page(self, path, oldpath):
		logger.debug('Updating links in page moved from %s to %s', oldpath, path)
		page = self.get_page(path)
		tree = page.get_parsetree()
		if not tree:
			return 0

		def replacefunc(elt):
			text = elt.attrib['href']
			if link_type(text) != 'page':
				raise zim.formats.VisitorSkip

			href = HRef.new_from_wiki_link(text)
			if href.rel == HREF_REL_FLOATING:
				newtarget = self.pages.resolve_link(page, href)
				oldtarget = self.pages.resolve_link(oldpath, href)

				if newtarget != oldtarget:
					return self._update_link_tag(elt, page, oldtarget, href)
				else:
					raise zim.formats.VisitorSkip
			else:
				raise zim.formats.VisitorSkip

		tree.replace(zim.formats.LINK, replacefunc)
		page.set_parsetree(tree)
		self.store_page(page)


	def _update_links_to_moved_page(self, oldtarget, newtarget):
		# 1. Check remaining placeholders, update pages causing them
		seen = set()
		try:
			oldtarget = self.pages.lookup_by_pagename(oldtarget)
		except IndexNotFoundError:
			pass
		else:
			for link in list(self.links.list_links_section(oldtarget, LINK_DIR_BACKWARD)):
				if link.source.name not in seen:
					self._move_links_in_page(link.source, oldtarget, newtarget)
					seen.add(link.source.name)

		# 2. Check for links that have anchor of same name as the moved page
		# and originate from a (grand)child of the parent of the moved page
		# and no longer resolve to the moved page
		# (these may have resolved to a higher level after the move)
		parent = oldtarget.parent
		for link in list(self.links.list_floating_links(oldtarget.basename)):
			if link.source.name not in seen \
			and link.source.ischild(parent) \
			and not (
				link.target.ischild(parent)
				or link.target == newtarget
				or link.target.ischild(newtarget)
			):
				self._move_links_in_page(link.source, oldtarget, newtarget)
				seen.add(link.source.name)

	def _move_links_in_page(self, path, oldtarget, newtarget):
		logger.debug('Updating page %s to move link from %s to %s', path, oldtarget, newtarget)
		page = self.get_page(path)
		tree = page.get_parsetree()
		if not tree:
			return 0

		def replacefunc(elt):
			text = elt.attrib['href']
			if link_type(text) != 'page':
				raise zim.formats.VisitorSkip

			href = HRef.new_from_wiki_link(text)
			target = self.pages.resolve_link(page, href)

			if target == newtarget or target.ischild(newtarget):
				raise zim.formats.VisitorSkip

			elif target == oldtarget:
				return self._update_link_tag(elt, page, newtarget, href)
			elif target.ischild(oldtarget):
				mynewtarget = newtarget.child( target.relname(oldtarget) )
				return self._update_link_tag(elt, page, mynewtarget, href)

			elif href.rel == HREF_REL_FLOATING \
			and href.parts()[0] == newtarget.basename \
			and page.ischild(oldtarget.parent) \
			and not target.ischild(oldtarget.parent):
				# Edge case: an link that was anchored to the moved page,
				# and now resolves somewhere higher in the tree
				if href.names == newtarget.basename:
					return self._update_link_tag(elt, page, newtarget, href)
				else:
					mynewtarget = newtarget.child(':'.join(href.parts[1:]))
					return self._update_link_tag(elt, page, mynewtarget, href)

			else:
				raise zim.formats.VisitorSkip

		tree.replace(zim.formats.LINK, replacefunc)
		page.set_parsetree(tree)
		self.store_page(page)

	def _update_link_tag(self, elt, source, target, oldhref):
		if oldhref.rel == HREF_REL_ABSOLUTE: # prefer to keep absolute links
			newhref = HRef(HREF_REL_ABSOLUTE, target.name)
		else:
			newhref = self.pages.create_link(source, target)

		text = newhref.to_wiki_link()
		if elt.gettext() == elt.get('href'):
			elt[:] = [text]
		elt.set('href', text)
		return elt

	def rename_page(self, path, newbasename, update_heading=True, update_links=True, callback=None):
		'''Rename page to a page in the same namespace but with a new
		basename.

		This is similar to moving within the same namespace, but
		conceptually different in the user interface. Internally
		L{move_page()} is used here as well.

		@param path: a L{Path} object for the old/current page name
		@param newbasename: new name as string
		@param update_heading: if C{True} the first heading in the
		page will be updated to the new name
		@param update_links: if C{True} all links B{from} and B{to} this
		page and any of it's children will be updated to reflect the
		new page name
		@param callback: see L{move_page()} for details
		'''
		logger.debug('Rename %s to "%s" (%s, %s)',
			path, newbasename, update_heading, update_links)

		newbasename = Path.makeValidPageName(newbasename)
		newpath = Path(path.namespace + ':' + newbasename)
		self.move_page(path, newpath, update_links, callback)
		if update_heading:
			page = self.get_page(newpath)
			tree = page.get_parsetree()
			if not tree is None:
				tree.set_heading(newbasename)
				page.set_parsetree(tree)
				self.store_page(page)

		return newpath

	def delete_page(self, path, update_links=True, callback=None):
		'''Delete a page from the notebook

		@param path: a L{Path} object
		@param update_links: if C{True} pages linking to the
		deleted page will be updated and the link are removed.
		@param callback: see L{move_page()} for details

		@returns: C{True} when the page existed and was deleted,
		C{False} when the page did not exist in the first place.

		Raises an error when delete failed.

		@emits: delete-page before the actual delete
		@emits: deleted-page after succesful deletion
		'''
		logger.debug('Delete page: %s', path)
		return self._delete_page(path, update_links, callback)

	def trash_page(self, path, update_links=True, callback=None):
		'''Move a page to Trash

		Like L{delete_page()} but will use the system Trash (which may
		depend on the OS we are running on). This is used in the
		interface as a more user friendly version of delete as it is
		undoable.

		@param path: a L{Path} object
		@param update_links: if C{True} pages linking to the
		deleted page will be updated and the link are removed.
		@param callback: see L{move_page()} for details

		@returns: C{True} when the page existed and was deleted,
		C{False} when the page did not exist in the first place.

		Raises an error when trashing failed.

		@raises TrashNotSupportedError: if trashing is not supported by
		the storage backend or when trashing is explicitly disabled
		for this notebook.

		@emits: delete-page before the actual delete
		@emits: deleted-page after succesful deletion
		'''
		logger.debug('Trash page: %s', path)
		if self.config['Notebook']['disable_trash']:
			raise TrashNotSupportedError, 'disable_trash is set'
		return self._delete_page(path, update_links, callback, trash=True)

	def _delete_page(self, path, update_links=True, callback=None, trash=False):
		assert callback is None # TODO TODO - iterator version

		# actual delete
		self.emit('delete-page', path)

		if trash:
			existed = self.store.trash_page(path)
		else:
			existed = self.store.delete_page(path)

		self.flush_page_cache(path)
		path = Path(path.name)

		self.index.on_delete_page(path)

		if not update_links:
			return

		# remove persisting links
		try:
			indexpath = self.pages.lookup_by_pagename(path)
		except IndexNotFoundError:
			return # no placeholder, we are done

		pages = set(
			l.source for l in self.links.list_links_section(path, LINK_DIR_BACKWARD) )

		for p in pages:
				page = self.get_page(p)
				self._remove_links_in_page(page, path)
				self.store_page(page)

		# let everybody know what happened
		self.emit('deleted-page', path)

		return existed

	def _remove_links_in_page(self, page, path):
		logger.debug('Removing links in %s to %s', page, path)
		tree = page.get_parsetree()
		if not tree:
			return

		def replacefunc(elt):
			href = elt.attrib['href']
			type = link_type(href)
			if type != 'page':
				raise zim.formats.VisitorSkip

			hrefpath = self.pages.lookup_from_user_input(href, page)
			#~ print 'LINK', hrefpath
			if hrefpath == path \
			or hrefpath.ischild(path):
				# Replace the link by it's text
				return zim.formats.DocumentFragment(*elt)
			else:
				raise zim.formats.VisitorSkip

		tree.replace(zim.formats.LINK, replacefunc)
		page.set_parsetree(tree)

	def resolve_file(self, filename, path=None):
		'''Resolve a file or directory path relative to a page or
		Notebook

		This method is intended to lookup file links found in pages and
		turn resolve the absolute path of those files.

		File URIs and paths that start with '~/' or '~user/' are
		considered absolute paths. Also windows path names like
		'C:\user' are recognized as absolute paths.

		Paths that starts with a '/' are taken relative to the
		to the I{document root} - this can e.g. be a parent directory
		of the notebook. Defaults to the filesystem root when no document
		root is set. (So can be relative or absolute depending on the
		notebook settings.)

		Paths starting with any other character are considered
		attachments. If C{path} is given they are resolved relative to
		the I{attachment folder} of that page, otherwise they are
		resolved relative to the I{notebook folder} - if any.

		The file is resolved purely based on the path, it does not have
		to exist at all.

		@param filename: the (relative) file path or uri as string
		@param path: a L{Path} object for the page
		@returns: a L{File} object.
		'''
		assert isinstance(filename, basestring)
		filename = filename.replace('\\', '/')
		if filename.startswith('~') or filename.startswith('file:/'):
			return File(filename)
		elif filename.startswith('/'):
			dir = self.document_root or Dir('/')
			return dir.file(filename)
		elif is_win32_path_re.match(filename):
			if not filename.startswith('/'):
				filename = '/'+filename
				# make absolute on Unix
			return File(filename)
		else:
			if path:
				dir = self.get_attachments_dir(path)
			else:
				assert self.dir, 'Can not resolve relative path for notebook without root folder'
				dir = self.dir

			return File((dir, filename))

	def relative_filepath(self, file, path=None):
		'''Get a file path relative to the notebook or page

		Intended as the counter part of L{resolve_file()}. Typically
		this function is used to present the user with readable paths or to
		shorten the paths inserted in the wiki code. It is advised to
		use file URIs for links that can not be made relative with
		this method.

		The link can be relative:
		  - to the I{document root} (link will start with "/")
		  - the attachments dir (if a C{path} is given) or the notebook
		    (links starting with "./" or "../")
		  - or the users home dir (link like "~/user/")

		Relative file paths are always given with Unix path semantics
		(so "/" even on windows). But a leading "/" does not mean the
		path is absolute, but rather that it is relative to the
		X{document root}.

		@param file: L{File} object we want to link
		@keyword path: L{Path} object for the page where we want to
		link this file

		@returns: relative file path as string, or C{None} when no
		relative path was found
		'''
		notebook_root = self.dir
		document_root = self.document_root

		# Look within the notebook
		if path:
			attachments_dir = self.get_attachments_dir(path)

			if file.ischild(attachments_dir):
				return './'+file.relpath(attachments_dir)
			elif document_root and notebook_root \
			and document_root.ischild(notebook_root) \
			and file.ischild(document_root) \
			and not attachments_dir.ischild(document_root):
				# special case when document root is below notebook root
				# the case where document_root == attachment_folder is
				# already caught by above if clause
				return '/'+file.relpath(document_root)
			elif notebook_root \
			and file.ischild(notebook_root) \
			and attachments_dir.ischild(notebook_root):
				parent = file.commonparent(attachments_dir)
				uppath = attachments_dir.relpath(parent)
				downpath = file.relpath(parent)
				up = 1 + uppath.count('/')
				return '../'*up + downpath
		else:
			if document_root and notebook_root \
			and document_root.ischild(notebook_root) \
			and file.ischild(document_root):
				# special case when document root is below notebook root
				return '/'+file.relpath(document_root)
			elif notebook_root and file.ischild(notebook_root):
				return './'+file.relpath(notebook_root)

		# If that fails look for global folders
		if document_root and file.ischild(document_root):
			return '/'+file.relpath(document_root)

		# Finally check HOME or give up
		return file.user_path or None

	def get_attachments_dir(self, path):
		'''Get the X{attachment folder} for a specific page

		@param path: a L{Path} object
		@returns: a L{Dir} object or C{None}

		Always returns a Dir object when the page can have an attachment
		folder, even when the folder does not (yet) exist. However when
		C{None} is returned the store implementation does not support
		an attachments folder for this page.
		'''
		folder = self.get_page(path).folder
		if not folder and self.store.__class__.__name__.startswith('Memory'):
			# XXX this is just to keep tests with "fakedir" happy :(
			from .stores import encode_filename
			dirpath = encode_filename(path.name)
			return self.dir.subdir(dirpath)
		else:
			return folder

	def get_template(self, path):
		'''Get a template for the intial text on new pages
		@param path: a L{Path} object
		@returns: a L{ParseTree} object
		'''
		# FIXME hardcoded that template must be wiki format

		template = self.namespace_properties[path]['template']
		logger.debug('Found template \'%s\' for %s', template, path)
		template = zim.templates.get_template('wiki', template)
		return self.eval_new_page_template(path, template)

	def eval_new_page_template(self, path, template):
		lines = []
		context = {
			'page': {
				'name': path.name,
				'basename': path.basename,
				'section': path.namespace,
				'namespace': path.namespace, # backward compat
			}
		}
		self.emit('new-page-template', path, template) # plugin hook
		template.process(lines, context)

		parser = zim.formats.get_parser('wiki')
		return parser.parse(lines)

	@property
	def needs_upgrade(self):
		'''Checks if the notebook is uptodate with the current zim version'''
		try:
			version = str(self.config['Notebook']['version'])
			version = tuple(version.split('.'))
			return version < DATA_FORMAT_VERSION
		except KeyError:
			return True
