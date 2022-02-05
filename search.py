#!/usr/bin/python
# -*- coding: utf-8 -*-

import webapp2
import logging
import jinja2
import os

from google.appengine.api import search
from google.appengine.ext import db

from user import auth
from page import Page


_INDEX_NAME="page"

def update_page_search(page_id):
	page = Page.get_by_id(page_id)
	# get last section of path
	path = page.path[page.path.rindex('/') + 1: ]
	# add to search index
	doc = search.Document(doc_id=str(page_id), 
						fields=[
							search.HtmlField(name='html', value=page.html),
							search.TextField(name='name', value=page.name),
							search.TextField(name='path', value=path),
						])
	try:
	    search.Index(name=_INDEX_NAME).put(doc)
	except search.Error:
	    logging.exception('Put failed')

def delete_page_search(page_id):
	# remove from index
	search.Index(name=_INDEX_NAME).delete(str(page_id))

def search_page(text):
	pages = []	
	if text:
		try:
		    keys = []
		    for doc in search.Index(name=_INDEX_NAME).search(text):
		    	keys.append(db.Key.from_path('Page', int(doc.doc_id)))
		    # bulk load pages
		    pages = db.get(keys)
		except search.Error:
		    logging.exception('Search failed')
	return pages



jinja_environment = jinja2.Environment(
    loader=jinja2.FileSystemLoader(os.path.dirname(__file__) + '/template/search'))

class SearchPage(webapp2.RequestHandler):
	@auth
	def get(self):
		text = self.request.get('text')
		docs = search_page(text)
		
		template = jinja_environment.get_template('search.html')
		self.response.out.write(template.render({'docs': docs, 'text': text}))



debug = os.environ.get('SERVER_SOFTWARE', '').startswith('Dev')

app = webapp2.WSGIApplication([('/.search', SearchPage),

								], debug=debug)