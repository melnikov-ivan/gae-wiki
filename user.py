#!/usr/bin/python
# -*- coding: utf-8 -*-

import webapp2
import logging
import jinja2
import os
from datetime import datetime

from google.appengine.ext import db
from google.appengine.api import users
from google.appengine.api import memcache


class User(db.Model):
	user_id = db.StringProperty()
	name = db.TextProperty()
	email = db.StringProperty()
	role = db.StringProperty()
	created = db.DateTimeProperty(auto_now_add=True)
	approved = db.DateTimeProperty()


log = logging.getLogger('user')

CACHE_USER = 'user:'
CACHE_USER_ID = 'user_id:'
CACHE_AUTH = 'auth:'

def get_user_by_id(user_id):
	key = CACHE_USER_ID + str(user_id)
	result = memcache.get(key)
	if not result:	
		result = User.get_by_id(int(user_id))
		memcache.set(key, result)
	return result

def get_user():		
	user = users.get_current_user()
	if not user:
		return None


	user_id = user.user_id()
	log.info('openID ' + user_id)
	
	key = CACHE_USER + user_id
	result = memcache.get(key)
	if result is None:
		log.info('Load user from DB')
		not_empty = User.all().filter('user_id', user_id).fetch(1)
		if not_empty:
			result = not_empty[0]
			memcache.set(key, result)
		else:
			# what to put in cache if user doesn't exist
			pass

	return result

def is_admin():
	return users.is_current_user_admin()

def auth(target):
	"""Decorator checks authorization

	if user is authorized set property
	else redirect to login page
	"""
	def authorized(self, *args, **kwargs):
		# allow everything for admin
		if not users.is_current_user_admin():
			# Get current logged in user
			user = users.get_current_user()
			if not user:
				self.redirect(login_url())
				log.info('Unauthorized user')
				return

			# Check user has been approved
			key = CACHE_AUTH+user.user_id()
			grant = memcache.get(key)
			if grant is None:
				one = User.all().filter('user_id', user.user_id()).filter('approved !=', None).fetch(1)
				grant = len(one) > 0
				memcache.set(key, grant)

			if not grant:
				self.redirect('/_docs/join.html')
				return

		# execute method
		return target(self, *args, **kwargs)
	
	return authorized

def login_url():
	return users.create_login_url()

def logout_url():
	return users.create_logout_url('/')

jinja_environment = jinja2.Environment(
    loader=jinja2.FileSystemLoader(os.path.dirname(__file__) + '/template/user'))

class AddUser(webapp2.RequestHandler):

	@auth
	def get(self):
		# get users to approve
		users = User.all().filter('approved', None).fetch(1000)

		template = jinja_environment.get_template('add.html')
		self.response.out.write(template.render({'users': users}))

	# Unauthorized access to add new user request
	def post(self):
		''' Create new user '''

		user = users.get_current_user()
		if user is None:
			self.redirect(login_url())
			return

		# Check first user
		# created = User.all().fetch(1)
		# if not created:
		# 	# Approve first admin user
		# 	id = user.user_id()
		# 	name = user.nickname()
		# 	email = user.email()
		# 	now = datetime.now()
		# 	admin = User(user_id=id, name=name, email=email, approved=now)
		# 	admin.put()

		# 	# update authorization cache
		# 	memcache.set(CACHE_AUTH+user.user_id(), True)

		# Check already exists
		one = User.all().filter('user_id', user.user_id()).fetch(1)
		if not one:
			u = User(user_id=user.user_id(), name=user.nickname(), email=user.email())
			u.put()

		self.redirect('/')

	@auth
	def put(self):
		# Approve user
		user_id = self.request.get('user_id')
		log.info('Approve user ' + user_id)

		user = get_user_by_id(int(user_id))
		user.approved = datetime.now()
		user.put()

		# update authorization cache
		memcache.set(CACHE_AUTH+user.user_id, True)


class ListUser(webapp2.RequestHandler):

	@auth
	def get(self):
		users = User.all().filter('approved !=', None).order('-approved').fetch(1000)

		template = jinja_environment.get_template('list.html')
		self.response.out.write(template.render({'users': users}))



class CreateApp(webapp2.RequestHandler):
	def get(self):

		app_name = self.request.get('name').lower()
		user = users.get_current_user()
		from main import create_app
		create_app(app_name, user)

		self.redirect('/')


debug = os.environ.get('SERVER_SOFTWARE', '').startswith('Dev')

app = webapp2.WSGIApplication([('/.user', AddUser),
							   ('/.user/all', ListUser),
							   ('/.user/create', CreateApp),
								], debug=debug)