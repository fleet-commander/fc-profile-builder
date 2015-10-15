#!/usr/bin/python
# -*- coding: utf-8 -*-
# vi:ts=2 sw=2 sts=2

# Copyright (C) 2014 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the licence, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this program; if not, see <http://www.gnu.org/licenses/>.
#
# Authors: Alberto Ruiz <aruiz@redhat.com>
#          Oliver Gutiérrez <ogutierrez@redhat.com>

# Python imports
import os
import json
import requests
import uuid
import logging

# Fleet commander imports
from collectors import GoaCollector, GSettingsCollector
from phial import Phial, JSONResponse


class AdminService(Phial):

    def __init__(self, name, config, vnc_websocket):

        routes = [
            (r'^static/(?P<path>.+)$',            ['GET'],    self.serve_static),
            # Workaround for bootstrap font path
            # ('^components/bootstrap/dist/font',  ['GET'],    self.font_files),
            ('^profiles/$',                       ['GET'],    self.profiles),
            ('^profiles/save/<id>',               ['POST'],   self.profiles_save),
            ('^profiles/add$',                    ['GET'],    self.profiles_add),
            ('^profiles/delete/<uid>',            ['GET'],    self.profiles_delete),
            ('^profiles/discard/<id>',            ['GET'],    self.profiles_discard),
            ('^profiles/(?P<profile_id>[-\w]+)$', ['GET'],    self.profiles_id),
            ('^changes',                          ['GET'],    self.changes),
            ('^changes/submit/(?P<name>[-\w]+)$', ['POST'],   self.changes_submit_name),
            ('^changes/select',                   ['POST'],   self.changes_select),
            ('^deploy/(?P<name>[-\w]+)$',         ['GET'],    self.deploy),
            ('^session/start$',                   ['POST'],   self.session_start),
            ('^session/stop$',                    ['GET'],    self.session_stop),
            ('^$',                                ['GET'],    self.index),
        ]
        super(AdminService, self).__init__(routes=routes)

        self.vnc_websocket = vnc_websocket
        self.collectors_by_name = {}
        self.current_session = {}
        self.custom_args = config

        self.templates_dir = os.path.join(config['data_dir'], 'templates')

    def check_for_profile_index(self):
        INDEX_FILE = os.path.join(self.custom_args['profiles_dir'], "index.json")
        if os.path.isfile(INDEX_FILE):
            return

        try:
            open(INDEX_FILE, 'w+').write(json.dumps([]))
        except OSError:
            logging.error('There was an error attempting to write on %s' % INDEX_FILE)

    # Views
    def index(self, request):
        return self.serve_html_template('index.html')

    def profiles(self, request):
        self.check_for_profile_index()
        return self.serve_static(request, 'index.json', basedir=self.custom_args['profiles_dir'])

    def profiles_id(self, request, profile_id):
        return self.serve_static(request, profile_id + '.json', basedir=self.custom_args['profiles_dir'])

    def profiles_save(self, request, id):
        def write_and_close(path, load):
            f = open(path, 'w+')
            f.write(load)
            f.close()

        changeset = self.current_session.get('changeset', None)
        uid = self.current_session.get('uid', None)

        if not uid or uid != id:
            return '{"status": "nonexistinguid"}', 403
        if not changeset:
            return '{"status"}: "/changes/select/ change selection has not been submitted yet in the current session"}', 403

        INDEX_FILE = os.path.join(self.custom_args['profiles_dir'], 'index.json')
        PROFILE_FILE = os.path.join(self.custom_args['profiles_dir'], id+'.json')

        data = request.get_json()

        if not isinstance(data, dict):
            return '{"status": "JSON request is not an object"}', 403
        if not all([key in data for key in ['profile-name', 'profile-desc', 'groups', 'users']]):
            return '{"status": "missing key(s) in profile settings request JSON object"}', 403

        profile = {}
        settings = {}
        groups = []
        users = []

        for name, collector in self.current_session['changeset'].items():
            settings[name] = collector.get_settings()

        groups = [g.strip() for g in data['groups'].split(",")]
        users = [u.strip() for u in data['users'].split(",")]
        groups = filter(None, groups)
        users = filter(None, users)

        profile["uid"] = uid
        profile["name"] = data["profile-name"]
        profile["description"] = data["profile-desc"]
        profile["settings"] = settings
        profile["applies-to"] = {"users": users, "groups": groups}
        profile["etag"] = "placeholder"

        self.check_for_profile_index()
        index = json.loads(open(INDEX_FILE).read())
        index.append({"url": id, "displayName": data["profile-name"]})

        del(self.current_session["uid"])
        del(self.current_session["changeset"])
        self.collectors_by_name.clear()

        write_and_close(PROFILE_FILE, json.dumps(profile))
        write_and_close(INDEX_FILE, json.dumps(index))

        return JSONResponse({ 'status': 'ok' })

    def profiles_add(self, request):
        return self.serve_html_template('profile.add.html')

    def profiles_delete(self, uid):
        INDEX_FILE = os.path.join(self.custom_args['profiles_dir'], 'index.json')
        PROFILE_FILE = os.path.join(self.custom_args['profiles_dir'], uid+'.json')

        try:
            os.remove(PROFILE_FILE)
        except:
            pass

        index = json.loads(open(INDEX_FILE).read())
        for profile in index:
            if (profile["url"] == uid):
                index.remove(profile)

        open(INDEX_FILE, 'w+').write(json.dumps(index))
        return JSONResponse({ 'status': 'ok' })

    def profiles_discard(self, request, id):
        if self.current_session.get('uid', None) == id:
            del(self.current_session["uid"])
            del(self.current_session["changeset"])
            return JSONResponse({ 'status': 'ok' })

        return JSONResponse({ 'status': 'profile %s not found' % id}, 403)

    def changes(self, request):
        # FIXME: Add GOA changes summary
        collector = self.collectors_by_name.get('org.gnome.gsettings', None)
        if collector:
            return JSONResponse(collector.dump_changes())
        return JSONResponse([], 403)

    # TODO: change the key from 'sel' to 'changes'
    # TODO: Handle GOA changesets
    def changes_select(self, request):
        data = request.get_json()

        if not isinstance(data, dict):
            return JSONResponse({"status": "bad JSON data"}, 403)

        if "sel" not in data:
            return JSONResponse({"status": "bad_form_data"}, 403)

        if 'org.gnome.gsettings' not in self.collectors_by_name:
            return JSONResponse({"status": "session was not started"}, 403)

        selected_indices = [int(x) for x in data['sel']]
        collector = self.collectors_by_name['org.gnome.gsettings']
        collector.remember_selected(selected_indices)

        uid = str(uuid.uuid1().int)
        self.current_session['uid'] = uid
        self.current_session['changeset'] = dict(self.collectors_by_name)
        self.collectors_by_name.clear()

        return JSONResponse({"status": "ok", "uuid": uid})

    # Add a configuration change to a session
    def changes_submit_name(self, request, name):
        if name in self.collectors_by_name:
            self.collectors_by_name[name].handle_change(request)
            return JSONResponse({"status": "ok"})
        else:
            return JSONResponse({"status": "namespace %s not supported or session not started"} % name, 403)

    def deploy(self, uid):
        return self.serve_html_template('deploy.html')

    def session_start(self, request):
        data = request.get_json()
        req = None

        if self.current_session.get('host', None):
            return JSONResponse({"status": "session already started"}, 403)

        if not data:
            return JSONResponse({"status": "Request data was not a valid JSON object"}, 403)

        if 'host' not in data:
            return JSONResponse({"status": "no host was specified in POST request"}, 403)

        self.current_session = {'host': data['host']}
        try:
            req = requests.get("http://%s:8182/session/start" % data['host'])
        except requests.exceptions.ConnectionError:
            return JSONResponse({"status": "could not connect to host"}, 403)

        self.vnc_websocket.stop()
        self.vnc_websocket.target_host = data['host']
        self.vnc_websocket.target_port = 5935
        self.vnc_websocket.start()

        self.collectors_by_name.clear()
        self.collectors_by_name['org.gnome.gsettings'] = GSettingsCollector()
        self.collectors_by_name['org.gnome.online-accounts'] = GoaCollector()

        return JSONResponse(req.content, req.status_code)

    def session_stop(self, request):
        host = self.current_session.get('host', None)

        if not host:
            return JSONResponse({"status": "there was no session started"}, 403)

        msg, status = ({"status": "could not connect to host"}, 403)
        try:
            req = requests.get("http://%s:8182/session/stop" % host)
            msg, status = (json.loads(req.content), req.status_code)
        except requests.exceptions.ConnectionError:
            pass

        self.vnc_websocket.stop()
        self.collectors_by_name.clear()

        if host:
            del(self.current_session['host'])

        return JSONResponse(msg, status)
