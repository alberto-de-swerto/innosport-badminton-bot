from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import json
import unittest
from unittest import mock

import sportsite
from attendance_editor import AttendanceService, ActionStore, AttendanceError, eligible_starts, editable_start
from attendance_report import save_report
from test_sportsite import TemporaryProject, START, DUE, PEOPLE, fake_connection
from test_no_shows import no_show_report
from violation_registry import ViolationRegistry


class FakeSport:
    def __init__(self):
        self.rows = [dict(PEOPLE[0],hours=2), dict(PEOPLE[1],hours=0)]
        self.extra = {3:{"student_id":3,"full_name":"Fictional Extra","email":"extra_test@example.invalid"}}
        self.posts = []
        self.flags = {"id":101,"group_id":17,"can_grade":True,"can_edit":True}
        self.behavior = "normal"
        self.accesses = []

    def find(self,start):
        return {"start":start.isoformat(),"title":"Badminton club","extendedProps":dict(self.flags)}

    def get(self,path,params=None):
        self.accesses.append((path,params))
        if path == "/api/training/101":
            return {"training":{"id":101,"group":{"id":17},"start":START.isoformat(),"end":(START+timedelta(hours=2)).isoformat()}}
        if path == "/api/attendance/101/grades":
            return {"group_id":17,"start":START.isoformat(),"academic_duration":2,
                    "grades":[dict(p,medical_group="do-not-store",email="private@example.invalid") for p in self.rows]}
        if path == "/api/attendance/suggest_student":
            return [{"value":"%s_%s_%s_private-medical_True"%(p["student_id"],p["full_name"],p["email"]),
                     "label":"%s (%s)"%(p["full_name"],p["email"])} for p in self.extra.values()
                    if params["term"].casefold() in (p["full_name"]+p["email"]).casefold()]
        raise AssertionError("Unexpected endpoint: "+path)

    def attendance(self,event,start):
        return deepcopy(self.rows)

    def mark_attendance(self,start,training_id,student_id,hours):
        self.posts.append((start,training_id,student_id,hours))
        if self.behavior == "rejected":
            raise sportsite.AttendanceWriteRejected("Rejected")
        if self.behavior == "timeout_before":
            raise sportsite.AttendanceWriteUncertain("Unknown")
        person = next((p for p in self.rows if p["student_id"]==student_id),None)
        if person is None:
            person = {k:self.extra[student_id][k] for k in ("student_id","full_name")}
            self.rows.append(person)
        person["hours"]=hours
        if self.behavior == "timeout_after":
            raise sportsite.AttendanceWriteUncertain("Unknown")


class EditorTests(TemporaryProject):
    def setUp(self):
        super().setUp()
        self.at=START+timedelta(hours=3)
        clock=mock.patch.object(sportsite.Config,"now",lambda _:self.at)
        clock.start()
        self.addCleanup(clock.stop)
        self.api=FakeSport()
        @contextmanager
        def connector(_):
            yield self.api
        self.service=AttendanceService(self.config,connector)
        self.store=self.service.store

    def token(self,ident=2,hours=1,expected=0,**kwargs):
        person=next((p for p in self.api.rows if p["student_id"]==ident),self.api.extra.get(ident))
        return self.store.issue(77,"set",date=START.date().isoformat(),person=person,
            training_id=101,group_id=17,expected=expected,hours=hours,**kwargs)

    def test_boundaries_and_future_sessions_never_write(self):
        for delta in (timedelta(microseconds=-1),timedelta(days=3,microseconds=1)):
            self.at=START+delta
            with self.assertRaises(AttendanceError):
                editable_start(self.config,START.date())
        self.at=START+timedelta(days=3)
        self.assertIn(START,eligible_starts(self.config))
        self.assertEqual(self.api.posts,[])

    def test_absolute_write_verified_and_duplicate_clicks_do_not_increment(self):
        token=self.token()
        result=self.service.write(token,77)
        self.assertEqual(result["hours"],1)
        self.assertEqual(len(self.api.posts),1)
        self.assertTrue(self.service.write(token,77)["replay"])
        self.assertEqual(len(self.api.posts),1)
        self.assertEqual(self.api.rows[1]["hours"],1)

    def test_owner_expiry_wrong_training_and_out_of_range_hours_fail_closed(self):
        token=self.token()
        with self.assertRaises(AttendanceError): self.service.write(token,88)
        self.at+=timedelta(minutes=16)
        with self.assertRaises(AttendanceError): self.service.write(token,77)
        token=self.token(hours=3)
        with self.assertRaises(AttendanceError): self.service.write(token,77)
        token=self.token()
        with self.store.locked() as state:
            state["actions"][token]["training_id"]=999
            self.store.save(state)
        with self.assertRaises(AttendanceError): self.service.write(token,77)
        self.assertEqual(self.api.posts,[])

    def test_time_window_is_checked_when_button_is_pressed(self):
        self.at=START+timedelta(days=3,minutes=-1)
        token=self.token()
        self.at+=timedelta(minutes=2)
        with self.assertRaises(AttendanceError): self.service.write(token,77)
        self.assertEqual(self.api.posts,[])

    def test_trainer_permissions_and_external_hours_changes_are_rechecked(self):
        token=self.token()
        self.api.flags["can_grade"]=False
        with self.assertRaises(sportsite.PermissionError): self.service.write(token,77)
        self.api.flags["can_grade"]=True
        self.api.rows[1]["hours"]=2
        with self.assertRaisesRegex(AttendanceError,"уже изменились"): self.service.write(token,77)
        self.assertEqual(self.api.posts,[])

    def test_partial_email_search_and_add_to_attendance_only(self):
        result=self.service.read("search",START.date().isoformat(),query="extra_t")
        self.assertEqual(len(result["matches"]),1)
        person=result["matches"][0]
        self.assertEqual(person["email"],"extra_test@example.invalid")
        self.assertNotIn("private-medical",json.dumps(result))
        token=self.token(ident=3,expected=None)
        self.service.write(token,77)
        self.assertEqual(self.api.posts[0][2:],(3,1))
        self.assertTrue(all('check_in' not in path and 'enroll' not in path for path,_ in self.api.accesses))

    def test_search_is_bounded_and_never_auto_selects_a_match(self):
        with self.assertRaises(AttendanceError): self.service.read("search",START.date().isoformat(),query="ex")
        self.assertEqual(self.api.posts,[])

    def test_uncertain_response_is_read_back_without_retrying_post(self):
        self.api.behavior="timeout_after"
        token=self.token()
        self.assertEqual(self.service.write(token,77)["hours"],1)
        self.service.write(token,77)
        self.assertEqual(len(self.api.posts),1)

    def test_unknown_write_is_never_reposted_and_can_recover_by_readback(self):
        self.api.behavior="timeout_before"
        token=self.token()
        with self.assertRaises(AttendanceError): self.service.write(token,77)
        with self.assertRaises(AttendanceError): self.service.write(token,77)
        self.assertEqual(len(self.api.posts),1)
        self.api.rows[1]["hours"]=1
        self.assertEqual(self.service.write(token,77)["hours"],1)
        self.assertEqual(len(self.api.posts),1)

    def test_reconciliation_failure_does_not_claim_website_save_failed(self):
        result=self.service.write(self.token(),77)
        self.assertEqual(result["reconciliation"],"pending")  # No baseline in this test.
        self.assertEqual(result["hours"],1)

    def test_final_mark_reconciles_and_removes_existing_no_show(self):
        sportsite.collect(self.config,START,True,clock=lambda:DUE,connector=fake_connection)
        ViolationRegistry(self.config).record(no_show_report())
        self.assertEqual(ViolationRegistry(self.config).rows()[0]["student_id"],2)
        self.at+=timedelta(seconds=1)
        result=self.service.write(self.token(),77)
        self.assertEqual(result["reconciliation"],"updated")
        self.assertEqual(ViolationRegistry(self.config).rows(),[])

    def test_ongoing_session_marks_do_not_create_final_violations(self):
        self.at=START+timedelta(minutes=15)
        result=self.service.write(self.token(),77)
        self.assertEqual(result["reconciliation"],"after_training")
        self.assertEqual(ViolationRegistry(self.config).rows(),[])

    def test_corrupted_journal_fails_without_overwriting_it(self):
        sportsite.atomic_write(self.store.path,'{"schema_version":99}')
        with self.assertRaises(AttendanceError): self.store.lookup("a"*16,77)
        self.assertEqual(self.store.path.read_text(),'{"schema_version":99}')
        self.assertEqual(self.api.posts,[])
