#!/usr/bin/python
# -*- coding: utf-8 -*-

import os
domain = 'localhost'
if os.getenv('SERVER_SOFTWARE', '').startswith('Google App Engine/'):
	domain = 'wikinote.me'
EMPTY_NAMESPACE = ''

import webapp2
import logging
import jinja2
import os
from datetime import datetime

from google.appengine.ext import db
from google.appengine.api import users
from google.appengine.api import namespace_manager
from google.appengine.api import taskqueue

from user import User


class App(db.Model):
	created = db.DateTimeProperty(auto_now_add=True)


log = logging.getLogger('main')

@db.transactional(xg=True)
def create_app(app_name, user):
	if not is_available(app_name):
		return

	# Create new app
	namespace_manager.set_namespace(EMPTY_NAMESPACE)
	app = App(key_name=app_name)
	app.put()
	log.info('Create new project: '+app_name)

	# Approve first admin user
	namespace_manager.set_namespace(app_name)
	id = user.user_id()
	name = user.nickname()
	email = user.email()
	now = datetime.now()
	admin = User(user_id=id, name=name, email=email, approved=now)
	admin.put()

	# Create guide page
	text = _WELCOME_GUIDE
	from page import create_page
	create_page('/', u'Главная', admin.key().id(), text)

	return True

def get_apps():
	namespace_manager.set_namespace(EMPTY_NAMESPACE)
	return App.all().order('-created').fetch(10)

def is_available(name):
	# Check project name availability
	if name in ['admin', 'www', 'demo', 'blog', '']:
		return False
	if len(name) < 3:
		return False

	namespace_manager.set_namespace(EMPTY_NAMESPACE)
	app = App.get_by_key_name(name)
	if app:
		return False

	return True


def migrate_all():
	'''Call migrate task in all namespaces'''
	# from main import EMPTY_NAMESPACE
	namespace_manager.set_namespace(EMPTY_NAMESPACE)
	log.info('iterate app names')
	for app in App.all():
		app_name = app.key().name()
		log.info('call migrate task in app '+app_name)
		taskqueue.add(url='/.task/migrate', params={'app': app_name})


# Guide on welcome page
_WELCOME_GUIDE=u'''
=Добро пожаловать

!!Внимание! Этот текст можно отредактировать.!!

==Возможности

Вики позволяет форматировать текст, используя специальные символы. WikiNote использует синтаксис Creole.

Ссылка на документацию по синтаксису указана внизу страницы.

==Создание
//Чтобы вернуться на эту страницу, достаточно нажать на ссылку wiki вверху страницы//
Для создания новой заметки, введите в адресной строке урл, где она будет находиться, например {{{ wikinote.me/test }}}.
Т.к. такой страницы еще не существует, то вам будет предложено ее создать - нажимаем "Создать". 
Введите текст, используя специальный язык форматирования, и нажмите сохранить - все ваша первая страница готова.

==Редактирование
Нажмите на кнопку 'Правка' внизу или дважды кликните по тексту. 
\\\\ Для создания новой страницы просто перейдите по ее адресу и отредактируйте.
===Заголовки
Обозначаются символом {{{'=', =Заголовок=}}}. Заголовок второго уровня {{{==Подзаголовок==}}}
===Ссылки
На страницу {{{ [[ /new_page | Текст ссылки ]] }}}

==Файлы
Нажмите на '+' в правом верхнем углу рядом с надписью 'Нет файлов'. Новый файл появится в выпадающем списке.
\\\\ Добавьте ссылку на файл {{{ [[ /ваша/страница/.files/имя-файла | Мой файл ]] }}}

===Картинки 
Вставьте картинку {{{ {{ http://урл/вашей/картинки.jpg }} }}}. 
\\\\ Картинка из списка прикреленных файлов. {{{ {{ /ваша/страница/.files/картинка.jpg }} }}}

==Больше возможностей
Посмотрите все возможности в выпадающем меню 'Правка'
'''

jinja_environment = jinja2.Environment(
    loader=jinja2.FileSystemLoader(os.path.dirname(__file__) + '/template/main'))

class Main(webapp2.RequestHandler):
	'''Root page'''

	def get(self):
		template = jinja_environment.get_template('index.html')
		self.response.out.write(template.render({}))


class Get(webapp2.RequestHandler):
	'''Get static pages from public site'''

	def get(self, page):
		try:
			template = jinja_environment.get_template(page+'.html')
			self.response.out.write(template.render({}))
		except Exception:
			self.error(404)


class CreateApp(webapp2.RequestHandler):
	'''Create new project'''
	
	def get(self):
		'''Ajax handler to check name availability'''
		name = self.request.get('name')
		name = name.lower()

		if is_available(name):
			result = 'Ура, имя свободно'
		else:
			result = 'Имя занято'
		self.response.out.write(result)


	def post(self):
		name = self.request.get('name')
		name = name.lower()

		# redirect
		if is_available(name): \
			redir = str('http://' + name + '.' + domain + '/.user/create?name=' + name)
		else: 
			redir = '/'
		self.redirect(redir)



main_routes = [

	('/', Main),
	('/create', CreateApp),
	(r'/<:[^/]+>', Get),

]


from admin import admin_routes
from page import page_routes

debug = os.environ.get('SERVER_SOFTWARE', '').startswith('Dev')

from webapp2_extras import routes

app = webapp2.WSGIApplication([

    routes.DomainRoute('admin.'+domain, [webapp2.Route(u, h) for u, h in admin_routes] ),
    # for cron tasks
    routes.DomainRoute('wiki-ee.appspot.com', [webapp2.Route(u, h) for u, h in admin_routes] ),
    
	routes.DomainRoute('www.'+domain, [webapp2.Route(u, h) for u, h in main_routes] ),
    
    routes.DomainRoute('<:[^.]+>.'+domain, [webapp2.Route(u, h) for u, h in page_routes] ),

	], debug=debug)