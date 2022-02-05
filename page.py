#!/usr/bin/python
# -*- coding: utf-8 -*-

import webapp2
import logging
import jinja2
import os
import urllib
import zlib
import json

from google.appengine.ext import db
from google.appengine.ext import blobstore
from google.appengine.ext.webapp import blobstore_handlers
from google.appengine.api import taskqueue
from google.appengine.api import memcache
from google.appengine.ext import deferred
from google.appengine.api import mail
from google.appengine.api import namespace_manager

from creole import Parser
from creole.html_emitter import HtmlEmitter

from user import User, get_user_by_id, get_user, is_admin, auth, login_url, logout_url

##################
###   Models   ###
##################

# Enum for page access policy
class Access():
	PRIVATE = 'PRIVATE'
	PARENT = 'PARENT'
	PUBLIC = 'PUBLIC'

# Enum for page markup language
class Markup():
	WIKI = 'WIKI'
	HTML = 'HTML'

# Main class to store wiki-page
class Page(db.Model):
	path = db.StringProperty(required=True)
	name = db.TextProperty()
	html = db.TextProperty() #required=True
	updated = db.DateTimeProperty(auto_now=True)
	user_id = db.IntegerProperty()
	f_cnt = db.IntegerProperty(default=0) # files counter
	access = db.StringProperty(default=Access.PARENT)

	# url path
	# for root return empty string to avoid //.edit urls
	def upath(self):
		p = self.path
		return p if p != '/' else ''

class History(db.Model):
	page = db.ReferenceProperty(reference_class=Page, collection_name='history', required=True)
	zipp = db.BlobProperty(required=True)
	markup = db.StringProperty(default=Markup.WIKI)
	updated = db.DateTimeProperty(auto_now=True)
	user_id = db.IntegerProperty()

	def html(self):
		return zlib.decompress(self.zipp).decode('utf-8')

class File(db.Model):
	page = db.ReferenceProperty(reference_class=Page, collection_name='files', required=True)
	user_id = db.IntegerProperty()
	name = db.StringProperty(required=True)
	size = db.IntegerProperty()
	date = db.DateTimeProperty()
	blob = blobstore.BlobReferenceProperty(required=True)
	# url = db.StringProperty()

class PageIndex(db.Model):
	''' Usefull to move page clusters and show subpages '''
	path = db.StringListProperty(required=True)
	# store random mark to determine 
	# we have already moved page in case of failure

class Subscribe(db.Model):
	page = db.ListProperty(item_type=int, default=[])
	cluster = db.ListProperty(item_type=int, default=[])

	def subscribe(self, page_id, t):
		if t == 'page':
			if page_id in self.cluster:
				self.cluster.remove(page_id)
			self.page.append(page_id)
		if t == 'cluster':
			if page_id in self.page:
				self.page.remove(page_id)
			self.cluster.append(page_id)
		if t == 'none':
			if page_id in self.page: 
				self.page.remove(page_id)
			if page_id in self.cluster:
				self.cluster.remove(page_id)


CACHE_PAGE = 'page:'
CACHE_FILES = 'files:'

#################
##    Pages    ##
#################

def create_page(path, name, user_id, text):
	''' Create new page '''
	page = Page(path=path, name=name, user_id=user_id)
	_update_page(page, text, markup=Markup.WIKI)

def _update_page(page, text, markup, history_id=None):
	new = False if page.is_saved() else True

	# update page content
	if markup == Markup.WIKI:
		html = convert(text)
	else:
		html = text
	page.html = html
	page.put()
	
	# update revision history
	zipp = zlib.compress(text.encode('utf-8'), 9)
	if history_id:
		history = History.get_by_id(history_id)
		history.zipp=zipp
	else:
		history = History(page=page.key(), zipp=zipp, user_id=page.user_id)
	history.markup = markup
	history.put()

	# update cache
	set_page(page.path, page)

	# add path index
	if new:
		deferred.defer(add_page_index, page.key().id(), page.path)

	# add to search index
	from search import update_page_search
	deferred.defer(update_page_search, page.key().id())

	# notify subscribers
	deferred.defer(notify_page, page.key().id())

	return history.key().id()


def _move_page(page, index, path):
	''' Update dependent entities '''

	# move page		
	page.path = path 
	page.put()
	# update index
	index.path = index_path(path)
	index.put()

	# clear page cache
	memcache.delete(CACHE_PAGE+page.path)

	# update search index
	from search import update_page_search
	deferred.defer(update_page_search, page.key().id())

	# TODO: notify subscribers
	# deferred.defer(notify_page, page.key().id())


def get_page(path):
	if not isinstance(path, unicode):
		path = unicode(path, 'utf-8')
	path = path.lower()

	key = CACHE_PAGE + path
	page = memcache.get(key)
	if page is None:
		log.info('Load page from DB')
		found = Page.all().filter('path', path).fetch(1)
		if found:
			page = found[0]
			memcache.set(key, page)
	return page

def set_page(path, page):
	if not isinstance(path, unicode):
		path = unicode(path, 'utf-8')
	path = path.lower()

	key = CACHE_PAGE + path
	memcache.set(key, page)

def get_files(page):
	if page.f_cnt == 0:
		return []

	key = CACHE_FILES + page.path
	files = memcache.get(key)
	if files is None:
		log.info('load files from DB')
		files = page.files.fetch(1000)
		memcache.set(key, files)
	return files

def delete_page_file(file):
	file.blob.delete()
	file.delete() # maybe just set flag removed

def breadcrumbs(path):
	path = unicode(path, 'utf-8')
	result = []
	full = ''
	for step in path.split('/'):
		if not step: continue
		full += '/'+step
		result.append((step, full))
	return result

def index_path(path):
	if path[0] != '/':
		raise Exception('path should start from slash: '+path)
	result = ['/']
	current = ''
	for step in path[1:].split('/'):
		# add non empty, maybe throw exception
		if not step: continue 

		current += '/'+step
		result.append(current)
	return result

def add_page_index(page_id, path):
	index = PageIndex(key=db.Key.from_path('PageIndex', page_id), path=index_path(path))
	index.put()

#######################
##   Notifications   ##
#######################

def notify_page(page_id):
	'''Notify subscribers about page update'''
	page = Page.get_by_id(page_id)
	author = User.get_by_id(page.user_id)
	from_email = author.email

	# notify page subscribers
	users = Subscribe.all(keys_only=True).filter('page', page_id)
	for user in users:
		u = User.get_by_id(user.id())
		_send_notification(u, page, from_email)
	
	# notify cluster subscribers
	for path in index_path(page.path):
		deferred.defer(_notify_path, path, page, from_email)

	
def _notify_path(path, page, from_email):
	'''Notify cluster subpages subscribers'''
	log.info('Notify users in %s cluster about page %s updated', path, page.path)
	cluster = get_page(path)
	if cluster is None: return

	users = Subscribe.all(keys_only=True).filter('cluster', cluster.key().id())
	for user in users:
		u = User.get_by_id(user.id())
		_send_notification(u, page, from_email)

def _send_notification(u, page, from_email):
	'''Send message for concrete user about page update'''
	log.info('Notify user %s about page %s updated', u.name, page.path)
	# create uniq task name
	name = '%d-%s-%d' % (page.key().id(), page.updated.strftime('%Y-%m-%d-%H-%M-%S'), u.key().id())
	try:
		taskqueue.add(url=URL_NOTIFY, name=name, params={'path':page.path, 'from_email':from_email, 'to_email':u.email})
	except taskqueue.TombstonedTaskError, taskqueue.TaskAlreadyExistsError:
		log.debug('User already notified')


##################
###   Server   ###
##################

def convert(text):
	document = Parser(text).parse()
	return HtmlEmitter(document).emit()

def dateformat(value, format='%d.%m.%Y %H:%M'):
    return value.strftime(format)

def size(n):
	if n is None: n = 0

	if n < 10**3:
		return '%d B' % n 
	if n < 10**6:
		if n < 10**4:
			return '%.1f KB' % (n/1000.0)
		else:
			return '%d KB' % (n/1000)
	if n < 10**9:
		if n < 10**7:
			return '%.1f MB' % (n/10.0**6)
		else:
			return '%d MB' % (n/10**6)

def user_name(user_id):
	return get_user_by_id(user_id).name

def encode(url):
	return urllib.quote(url.encode('utf-8'))

def not_empty(l):
	return len(l) > 0

def check_access(page):
	'''Check current user can access this page

	* User may be not authorized
	* Page may not Exist
	'''

	if is_admin():
		return True
		
	if page is None:
		return get_user() is not None

	if Access.PUBLIC == page.access:
		return True

	if Access.PARENT == page.access:
		parents = index_path(page.path)
		if len(parents) == 1: 
			return get_user() is not None
		parents.reverse()
		# for path in parents[1]:
		path = parents[1]
		page = get_page(path)
		return check_access(page)
		
		# return True

	if Access.PRIVATE == page.access:
		user = get_user()
		# TODO: store private user list in separate entity
		if user and user.key().id() == page.user_id:
			return True
		else:
			log.info('No access to "%s"', page.path)
			return False

	raise Exception('Unsupported access type '+page.access)



jinja_environment = jinja2.Environment(
    loader=jinja2.FileSystemLoader(os.path.dirname(__file__) + '/template/page'))
jinja_environment.globals['logout'] = logout_url()
jinja_environment.filters['format'] = dateformat
jinja_environment.filters['size'] = size
jinja_environment.filters['user_name'] = user_name
jinja_environment.filters['urlencode'] = encode
jinja_environment.tests['not_empty'] = not_empty
jinja_environment.tests['access'] = check_access


log = logging.getLogger('page')
from admin import log_stat

class GetPage(webapp2.RequestHandler):
	''' Get formated wiki page '''
	
	# Unauthorized users can get PUBLIC pages
	def get(self, path):
		if not path: path = '/'
		page = get_page(path)
		
		# User may be not authorized
		if not check_access(page):
			self.redirect(login_url())

		# logs for metrics
		log_stat('GetPage')

		values = {'page': page, 'breadcrumbs': breadcrumbs(path)}
		if page:
			values['files'] = get_files(page)
			# TODO: lazy load
			values['upload'] = blobstore.create_upload_url(encode(page.upath()+'/.files'))
		if not get_user():
			values['public'] = True
		
		template = jinja_environment.get_template('get.html')
		self.response.out.write(template.render(values))

	@auth
	def post(self):
		'''Preview formated page while editing wiki'''
		text = self.request.get('text')
		html = convert(text)
		self.response.out.write(html)


class EditPage(webapp2.RequestHandler):
	''' Edit wiki page '''
	@auth
	def get(self, path):
		if not path: path = '/'
		page = get_page(path)
		name = self.request.get('name')

		values = {'page': page, 'breadcrumbs': breadcrumbs(path), 'name': name}
		if page:
			# TODO: lazy load
			values['files'] = get_files(page)
			values['upload'] = blobstore.create_upload_url(encode(page.upath()+'/.files'))

			history = page.history.order('-updated').fetch(1)[0]
			values['text'] = history.html()
			values['markup'] = history.markup
			
		template = jinja_environment.get_template('edit.html')
		self.response.out.write(template.render(values))

	@auth
	def post(self, path):
		if not path: path = '/'
		path = path.lower()
		page = get_page(path)

		# logs for metrics
		log_stat('EditPage')

		if not page:
			page = Page(path=unicode(path, 'utf-8'))

		name = self.request.get('name')
		text = self.request.get('text')
		markup = self.request.get('markup')
		history = self.request.get('history')
		
		
		user = get_user()
		page.user_id = user.key().id()
		page.name = name

		history_id = int(history) if history else 0
		_update_page(page, text, markup, history_id)

		self.redirect(encode(page.path))

	# Ajax calls
	@auth
	def put(self, path):
		if not path: path = '/'
		path = path.lower()
		page = get_page(path)

		# logs for metrics
		log_stat('EditPage')
		
		form = json.loads(self.request.body)
		name = form['name']
		text = form['text']
		markup = Markup.WIKI # form['markup']
		updated = form['updated']
		history = form['history']


		if not page:
			page = Page(path=unicode(path, 'utf-8'))
		elif page.updated.strftime('%s') != updated:
			# TODO: add message
			log.info('Page is not up to date %d',int(page.updated.strftime('%s')))
			return

		user = get_user()
		page.user_id = user.key().id()
		page.name = name

		history_id = int(history) if history else 0
		history_id = _update_page(page, text, markup, history_id)

		result = {'history_id': history_id, 'updated': int(page.updated.strftime('%s'))}
		self.response.out.write(json.dumps(result))


class DeletePage(webapp2.RequestHandler):
	''' Delete page '''
	@auth
	def get(self, path):
		if not path: path = '/'
		page = get_page(path)

		values = {'page': page, 'breadcrumbs': breadcrumbs(path)}
		if page:
			# TODO: lazy load
			values['files'] = get_files(page)
			values['upload'] = blobstore.create_upload_url(encode(page.upath()+'/.files'))
			
		template = jinja_environment.get_template('delete.html')
		self.response.out.write(template.render(values))

	def post(self, path):
		if not path: path = '/'
		page = get_page(path)

		files = page.files.fetch(1000)

		page_id = page.key().id()
		index = PageIndex.get_by_id(page_id)
		page.delete()
		index.delete()

		memcache.delete(CACHE_PAGE+path)

		from search import delete_page_search
		deferred.defer(delete_page_search, page_id)

		# delete files
		deferred.defer(_delete_page_files, files)

		# TODO: notify subscribers

		self.redirect('/')

# bulk delete files if defered queue
def _delete_page_files(files):
	for f in files:
		delete_page_file(f)


class HistoryPage(webapp2.RequestHandler):
	''' Show page revision history '''
	@auth
	def get(self, path):
		if not path: path = '/'
		page = get_page(path)
		history = page.history.order('-updated')

		values = {
			'page': page, 
			'history': history, 
			'breadcrumbs': breadcrumbs(path),
		}
		template = jinja_environment.get_template('history.html')
		self.response.out.write(template.render(values))


class ShowHistoryPage(webapp2.RequestHandler):
	''' Show page revision '''
	@auth
	def get(self, path):
		if not path: path = '/'
		page = get_page(path)
		history = History.get_by_id(int(self.request.get('id')))
		if history.page.key().id() != page.key().id(): return

		values = {
			'page': page, 
			'history': history, 
			'html': convert(history.html()),
			'breadcrumbs': breadcrumbs(path),
			'user': get_user_by_id(history.user_id)
		}
		template = jinja_environment.get_template('show.html')
		self.response.out.write(template.render(values))


class MovePage(webapp2.RequestHandler):
	''' Move to another path either single page or the whole cluster '''
	@auth
	def get(self, path):
		if not path: path = '/'
		page = get_page(path)

		template = jinja_environment.get_template('move.html')
		self.response.out.write(template.render({'page': page, 'files': get_files(page), 'breadcrumbs': breadcrumbs(path)}))

	def post(self, path):
		if not path: path = '/'
		page = get_page(path)
		
		index = PageIndex.get_by_id(page.key().id())
		new_path = self.request.get('new_path')

		_move_page(page, index, new_path)

		# move whole cluster
		if self.request.get('cluster'):
			taskqueue.add(url=URL_MOVE_PAGE, params={'from': path, 'to': new_path})

		self.redirect(encode(page.path))


URL_MOVE_PAGE = '/.task/move'
URL_NOTIFY = '/.task/notify'

class MovePageCluster(webapp2.RequestHandler):
	''' Move cluster pages to new path '''

	def post(self):
		from_path = self.request.get('from')
		to_path = self.request.get('to')
		if from_path and to_path:
			log.info('move cluster from "%s" to "%s"', from_path, to_path)
			indexes = PageIndex.all().filter('path', from_path)
			for index in indexes:
				page_id = index.key().id()
				page = Page.get_by_id(page_id)

				# update path
				relative = page.path[len(from_path):]
				path = to_path + relative

				_move_page(page, index, path)


class TreePage(webapp2.RequestHandler):
	''' View subpages '''
	@auth
	def get(self, path):
		if not path: path = '/'
		page = get_page(path)
		
		# bulk load pages by keys
		keys = []
		for index in PageIndex.all().filter('path', page.path):
			keys.append(db.Key.from_path('Page', index.key().id()))
		cluster = db.get(keys)

		# sort pages by path
		def sort_by_path(page):
			return page.path
		cluster.sort(key=sort_by_path)

		values = {
			'page': page,
			'files': get_files(page),
			'cluster': cluster,
			'breadcrumbs': breadcrumbs(path),
		}
		template = jinja_environment.get_template('cluster.html')
		self.response.out.write(template.render(values))


class SubscribePage(webapp2.RequestHandler):
	''' Subscribe to get notifications about page updates '''
	@auth
	def get(self, path):
		if not path: path = '/'
		page = get_page(path)

		values = {'page': page, 'breadcrumbs': breadcrumbs(path)}
		if page:
			# TODO: lazy load
			values['files'] = get_files(page)
			values['upload'] = blobstore.create_upload_url(encode(page.upath()+'/.files'))

			subscribers = []
			user_ids = Subscribe.all(keys_only=True).filter('page', page.key().id())
			for user in user_ids:
				subscribers.append(get_user_by_id(user.id()))
			values['subscribers'] = subscribers

			user = get_user()
			s = Subscribe.get_by_id(user.key().id())
			if s is not None:
				if page.key().id() in s.cluster:
					values['type'] = 'cluster'
				elif page.key().id() in s.page:
					values['type'] = 'page'
				else:
					values['type'] = 'none'


		template = jinja_environment.get_template('subscribe.html')
		self.response.out.write(template.render(values))

	def post(self, path):
		if not path: path = '/'
		page = get_page(path)
		if not page: return

		
		user = get_user()
		user_id = user.key().id()
		s = Subscribe.get_by_id(user_id)
		if not s:
			s = Subscribe(key=db.Key.from_path('Subscribe', user_id))

		page_id = page.key().id()
		t = self.request.get('type')
		s.subscribe(page_id, t)
		s.put()

		self.redirect(path)


class NotifyPageUpdate(webapp2.RequestHandler):
	'''Send notification to page subscriber'''
	def post(self):
		path = self.request.get('path')
		from_email = self.request.get('from_email')
		to_email = self.request.get('to_email')

		from main import domain
		log.info("Host name: %s.%s", namespace_manager.get_namespace(), domain)
		path = namespace_manager.get_namespace() +'.'+ domain + path
		subject = 'Page "%s" updated' % path
		msg = '''
			User %s updated page %s
			''' % (from_email, path)
		
		mail.send_mail(sender='admin@wikinote.me', to=to_email, subject=subject, body=msg)


class AccessPage(webapp2.RequestHandler):
	def get(self, path):
		if not path: path = '/'
		page = get_page(path)

		values = {'page': page, 'breadcrumbs': breadcrumbs(path)}
		if page:
			# TODO: lazy load
			values['files'] = get_files(page)
			values['upload'] = blobstore.create_upload_url(encode(page.upath()+'/.files'))

		template = jinja_environment.get_template('access.html')
		self.response.out.write(template.render(values))

	def post(self, path):
		if not path: path = '/'
		page = get_page(path)

		param = self.request.get('access')
		if 'private' == param:
			access = Access.PRIVATE
		if 'parent' == param:
			access = Access.PARENT
		if 'public' == param:
			access = Access.PUBLIC
		
		page.access = access
		page.user_id = get_user().key().id()
		page.put()

		# Update page cache
		set_page(path, page)

		self.redirect(path)


class EditPageFiles(blobstore_handlers.BlobstoreUploadHandler, blobstore_handlers.BlobstoreDownloadHandler):

	def get(self, path, name):
		'''Get page file by name'''

		if not path: path = '/'
		page = get_page(path)

		# User may be not authorized
		if not check_access(page):
			self.redirect(login_url())

		# logs for metrics
		log_stat('GetFile')

		name = unicode(name, 'utf-8')
		if page:
			files = page.files.filter('name', name).fetch(1)
			if files:
				self.send_blob(files[0].blob)
				return
		
		self.error(404)

	@auth	
	def post(self, path):
		''' Upload page file '''
		if not path: path = '/'
		page = get_page(path)

		upload_files = self.get_uploads('data')  # 'data' is file upload field in the form
		blob_info = upload_files[0]

		user = get_user()

		name = unicode(blob_info.filename, 'utf-8')
		size = blob_info.size
		date = blob_info.creation
		data = File(page=page.key(), blob=blob_info.key(), user_id=user.key().id(), name=name, size=size, date=date)
		data.put()

		# increment files counter in Page
		page.f_cnt = page.f_cnt + 1
		page.put()
		# update cache
		set_page(page.path, page)

		memcache.delete(CACHE_FILES + path)

		deferred.defer(notify_page, page.key().id())

		self.redirect(path)

	@auth
	def put(self, path, name):
		''' Delete page file by name '''
		if not path: path = '/'
		page = get_page(path)
		name = unicode(name, 'utf-8')

		files = page.files.filter('name', name).fetch(1)
		if files:
			delete_page_file(files[0])

			# decrease files counter in Page
			page.f_cnt = page.f_cnt - 1
			page.put()
			# update cache
			set_page(page.path, page)

			memcache.delete(CACHE_FILES + path)
			deferred.defer(notify_page, page.key().id())


# Migration

def migrate():
	from search import update_page_search
	for page in Page.all():
		deferred.defer(update_page_search, page.key().id())


page_routes = [

	(URL_MOVE_PAGE, MovePageCluster),
	(URL_NOTIFY, NotifyPageUpdate),

	('/.preview', GetPage),
	('<:.*>/.edit', EditPage),
	('<:.*>/.delete', DeletePage),
	('<:.*>/.log', HistoryPage),
	('<:.*>/.show', ShowHistoryPage),
	('<:.*>/.move', MovePage),
	('<:.*>/.tree', TreePage),
	('<:.*>/.subscribe', SubscribePage),
	('<:.*>/.access', AccessPage),

	('<:.*>/.files', EditPageFiles),
	('<:.*>/.files/<:.*>', EditPageFiles),
							   
	(u'<:.*>', GetPage),

]