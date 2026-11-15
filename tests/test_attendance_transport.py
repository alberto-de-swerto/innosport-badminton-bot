from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from unittest import mock

import sportsite
from test_sportsite import TemporaryProject, START


class MarkTransportTests(TemporaryProject):
    def setUp(self):
        super().setUp()
        self.at=START+timedelta(hours=1)
        self.csrf="f"*32
        self.calls=[]
        self.status=200
        parent=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                parent.calls.append(("GET",self.path,None))
                self.send_response(200)
                self.send_header("Set-Cookie","csrftoken="+parent.csrf+"; Path=/")
                self.end_headers()
                self.wfile.write(b'<html>fictional profile</html>')
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                parent.calls.append(("POST",self.path,{"body":body,"csrf":self.headers.get('X-CSRFToken'),
                                  "cookie":self.headers.get('Cookie'),"authorization":self.headers.get('Authorization')}))
                self.send_response(parent.status)
                self.send_header('Content-Type','application/json')
                if parent.status==302: self.send_header('Location','/must-not-follow')
                self.end_headers()
                self.wfile.write(b'[]')
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        for name,value in (("ORIGIN","http://127.0.0.1:%s"%server.server_port),("HOST","127.0.0.1")):
            patch=mock.patch.object(sportsite,name,value)
            patch.start()
            self.addCleanup(patch.stop)
        clock=mock.patch.object(sportsite.Config,'now',lambda _:self.at)
        clock.start()
        self.addCleanup(clock.stop)
        from playwright.sync_api import sync_playwright
        playwright=sync_playwright().start()
        self.addCleanup(playwright.stop)
        request=playwright.request.new_context()
        self.addCleanup(request.dispose)
        self.api=sportsite.SportAPI(request,self.config)

    def test_csrf_cookie_and_absolute_single_person_json_go_to_only_mark_endpoint(self):
        self.api.mark_attendance(START,101,2,1)
        method,path,data=self.calls[-1]
        self.assertEqual((method,path),('POST','/api/attendance/mark'))
        self.assertEqual(data['body'],{'training_id':101,'students_hours':[{'student_id':2,'hours':1}]})
        self.assertEqual(data['csrf'],self.csrf)
        self.assertIn('csrftoken='+self.csrf,data['cookie'])

    def test_bearer_mode_does_not_require_a_profile_or_csrf_cookie(self):
        self.api.bearer_token='fictional-bearer'
        self.api.mark_attendance(START,101,2,2)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0][2]['authorization'],'Bearer fictional-bearer')

    def test_future_and_expired_sessions_never_post(self):
        for now in (START-timedelta(seconds=1),START+timedelta(days=3,seconds=1)):
            self.at=now
            with self.assertRaises(sportsite.AttendanceWriteRejected): self.api.mark_attendance(START,101,2,1)
        self.assertFalse(any(c[0]=='POST' for c in self.calls))

    def test_redirect_and_rejection_are_not_followed_or_reported_as_success(self):
        for status in (302,400,403):
            self.status=status
            with self.assertRaises(sportsite.AttendanceWriteRejected): self.api.mark_attendance(START,101,2,1)
        self.assertFalse(any(c[1]=='/must-not-follow' for c in self.calls))

    def test_server_error_is_uncertain_and_transport_does_not_retry(self):
        self.status=500
        with self.assertRaises(sportsite.AttendanceWriteUncertain): self.api.mark_attendance(START,101,2,1)
        self.assertEqual(sum(c[0]=='POST' for c in self.calls),1)
