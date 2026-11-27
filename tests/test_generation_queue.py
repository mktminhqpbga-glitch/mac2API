"""Offline scheduling, budget, persistence and provenance regressions."""
import json
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
HOME = tempfile.TemporaryDirectory(prefix='muse-scheduler-test-')
os.environ.update(MUSE2API_HOME=HOME.name, MUSE2API_PROFILE_ROOT=HOME.name,
                  MUSE2API_KEY='scheduler-test', MUSE2API_AUTO_UPDATE='0')
import app
import store
from generation_timing import QueuePolicy, QueueSettings, TaskTiming



class GenerationQueueTests(unittest.TestCase):
    def setUp(self):
        app.store.accounts=[];app.store.tasks={}
        self.accounts=[app.store.add_account({"hatch_sess":n},n) for n in ["a","b","c"]]
        policy=QueuePolicy(str(Path(HOME.name)/"queue-test.json"));policy.save(QueueSettings())
        self.patches=[patch.object(app,"QUEUE_POLICY",policy),patch.object(app,"GEN_LOCK",app.ConcurrencyLock(2)),
          patch.object(app,"ACCOUNT_POOL",app.AccountPoolManager(app.store)),patch.object(app,"_renew_and_persist",return_value=None),
          patch.object(app,"_sync_cookies"),patch.object(app.engine,"start"),patch.object(app.engine,"close_current_page"),patch.object(app.engine,"reset_thread")]
        for p in self.patches:p.start()
        self.addCleanup(lambda:[p.stop() for p in reversed(self.patches)])

    def test_queue_wait_does_not_consume_generation_budget(self):
        clock=[0.0];seen={};real_pool=app.ACCOUNT_POOL
        class QueuedLock:
            def acquire(self,timeout):clock[0]+=515;return True
            def release(self):seen['released']=True
        def lease(**kwargs):clock[0]+=20;return real_pool.acquire(**kwargs)
        def generate(*args,**kwargs):seen['timeout']=kwargs['timeout'];return {'filename':'x.mp4'}
        queues=[]
        with patch.object(app,'time',SimpleNamespace(monotonic=lambda:clock[0],time=time.time)), \
             patch.object(app,'GEN_LOCK',QueuedLock()),patch.object(real_pool,'acquire',wraps=real_pool.acquire) as acquire, \
             patch.object(app.engine,'generate',side_effect=generate):
            original=acquire._mock_wraps
            def delayed(**kwargs):clock[0]+=20;return original(**kwargs)
            acquire.side_effect=delayed
            app._run_generation('x','video',600,on_queue=queues.append)
        self.assertEqual(seen['timeout'],600)
        self.assertEqual(queues[-1],535)
        self.assertTrue(seen['released']);self.assertFalse(real_pool.busy_accounts)

    def test_retry_account_wait_does_not_consume_generation_budget(self):
        clock=[0.0];timeouts=[];real_acquire=app.ACCOUNT_POOL.acquire
        def lease(**kwargs):clock[0]+=50 if not timeouts else 90;return real_acquire(**kwargs)
        def generate(*args,**kwargs):
            timeouts.append(kwargs['timeout'])
            if len(timeouts)==1:clock[0]+=30;raise app.MuseAuthError('expired login')
            return {'filename':'x.mp4'}
        with patch.object(app,'time',SimpleNamespace(monotonic=lambda:clock[0],time=time.time)), \
             patch.object(app.ACCOUNT_POOL,'acquire',side_effect=lease),patch.object(app.engine,'generate',side_effect=generate):
            app._run_generation('x','video',600)
        self.assertEqual(timeouts,[600,570])

    def test_original_timeout_is_not_overwritten(self):
        clock=[0.0];calls=[]
        def generate(*args,**kwargs):clock[0]+=605;calls.append(1);raise app.MuseGenerationError('original upstream timeout')
        with patch.object(app,'time',SimpleNamespace(monotonic=lambda:clock[0],time=time.time)),patch.object(app.engine,'generate',side_effect=generate):
            with self.assertRaisesRegex(app.MuseGenerationError,'original upstream timeout'):
                app._run_generation('x','video',600)
        self.assertEqual(calls,[1]);self.assertFalse(app.GEN_LOCK.locked());self.assertFalse(app.ACCOUNT_POOL.busy_accounts)

    def test_account_write_failure_after_generation_does_not_resubmit(self):
        result = {"filename": "fixture.mp4"}
        with patch.object(app.engine, "generate", return_value=result) as generate, \
             patch.object(app.store, "mark", side_effect=OSError("fixture disk full")):
            received, _account = app._run_generation("fixture", "video", 5)
        self.assertEqual(received, result)
        generate.assert_called_once()
        self.assertFalse(app.GEN_LOCK.locked())
        self.assertFalse(app.ACCOUNT_POOL.busy_accounts)

    def test_queue_failure_never_generates(self):
        with patch.object(app.GEN_LOCK,'acquire',return_value=False),patch.object(app.engine,'generate') as generate:
            with self.assertRaisesRegex(app.MuseGenerationError,'排队时限'):app._run_generation('x','video',1)
        generate.assert_not_called()

    def test_failed_job_keeps_account_and_durations(self):
        with patch.object(app.engine,'generate',side_effect=app.MuseModelRefusalError('只有文字，无媒体')):
            result=asyncio.run(app.create_video(app.VideoRequest(prompt='x')))
            until=time.monotonic()+2
            while app.store.get_task(result['id'])['status'] in ['queued','processing'] and time.monotonic()<until:time.sleep(.01)
        task=app.store.get_task(result['id'])
        self.assertEqual(task['status'],'failed');self.assertIn(task['account'],[a['id'] for a in self.accounts])
        self.assertIn('只有文字',task['last_error']);self.assertEqual(task['attempt'],1)
        self.assertGreaterEqual(task['total_elapsed'],task['elapsed']);self.assertIn('queue_elapsed',task)

    def test_corrupt_queue_file_uses_defaults(self):
        path = Path(HOME.name) / "corrupt-queue.json"
        path.write_text("{unreadable")
        self.assertEqual(QueuePolicy(path).settings(), QueueSettings().model_dump())
        self.assertEqual(path.read_text(), "{unreadable")

    def test_queue_save_failure_keeps_previous_settings(self):
        previous = app.QUEUE_POLICY.settings()
        stored = app.QUEUE_POLICY.path.read_bytes()
        with patch("generation_timing._write", side_effect=OSError("fixture disk full")):
            with self.assertRaises(app.HTTPException) as error:
                app.save_generation_queue(QueueSettings(queue_timeout=45))
        self.assertEqual(error.exception.status_code, 507)
        self.assertEqual(app.QUEUE_POLICY.settings(), previous)
        self.assertEqual(app.QUEUE_POLICY.path.read_bytes(), stored)

    def test_disk_error_does_not_reserve_new_task_or_stall_existing_task(self):
        task = app.store.create_task("image", "accepted")
        with patch.object(store, "_write", side_effect=OSError("fixture disk full")):
            with self.assertRaises(app.HTTPException) as error:
                app._create_media_task("video", "not accepted")
            self.assertEqual(error.exception.status_code, 507)
            app.store.update_task(task["id"], status="completed")
        self.assertEqual(len(app.store.tasks), 1)
        self.assertEqual(app.store.get_task(task["id"])["status"], "completed")

    def test_worker_launch_failure_returns_terminal_task(self):
        task = app.store.create_task("image", "fixture")
        with patch.object(app.threading.Thread, "start", side_effect=RuntimeError("fixture launch failure")):
            failed = app._start_media_worker(task, None, lambda: self.fail("worker must not run"))
        self.assertEqual(failed["id"], task["id"])
        self.assertEqual(app.store.get_task(task["id"])["status"], "failed")

    def test_restart_restores_queued_media_without_resending_inflight_work(self):
        retained = app.store.create_client("retained fixture", ["muse"], history_days=1)
        no_history = app.store.create_client("private fixture", ["muse"], history_days=0)
        image = app.ImageRequest(prompt="fixture image", model="muse/image", timeout=30)
        video = app.VideoRequest(prompt="fixture video", model="muse/video", timeout=40)
        reference = "data:image/png;base64,ZmFrZQ=="
        hold = lambda task, *_args: task
        with patch.object(app, "_start_media_worker", side_effect=hold), \
             patch.object(app, "_prepare_reference_images", return_value=[reference]):
            img_id = json.loads(app.create_image_task(image, "restart-image", _=retained).body)["id"]
            vid_id = asyncio.run(app.create_video(video, _=True))["id"]
            private_id = json.loads(app.create_image_task(image, "restart-private", _=no_history).body)["id"]
            sent_id = asyncio.run(app.create_video(video, _=True))["id"]
            cancelled_id = asyncio.run(app.create_video(video, _=True))["id"]
        app.store.transition_task(sent_id, "queued", status="processing", started_at=time.time())
        app._cancel_queued_task(cancelled_id)
        self.assertTrue(app.PENDING_MEDIA.exists(img_id))
        self.assertFalse(app.PENDING_MEDIA.exists(private_id))
        self.assertFalse(app.PENDING_MEDIA.exists(cancelled_id))
        self.assertNotIn(reference, Path(app.CFG.tasks_file).read_text())

        def run_inline(task, owner, worker, _job=None):
            app._generate_owned(owner, worker)
            return task

        def generate(prompt, kind, timeout, *, reference_images, on_acquired, **options):
            self.assertIn("fixture " + kind, prompt)
            self.assertEqual(reference_images, [reference])
            self.assertEqual(timeout, 30 if kind == "image" else 40)
            self.assertIn("already_queued", options)
            on_acquired()
            return {"filename": f"{kind}.mock", "size": 12, "kind": kind}, self.accounts[0]["id"]

        reopened = store.Store(app.CFG)
        with patch.object(app, "store", reopened), patch.object(app, "_start_media_worker", side_effect=run_inline), \
             patch.object(app, "_run_generation", side_effect=generate) as worker:
            queued = app._recover_media_tasks()
            self.assertEqual({task["id"] for task in queued}, {img_id, vid_id})
            app.client_usage.reconcile(reopened.tasks, restarting=True, resuming={img_id, vid_id})
            self.assertEqual(app.client_usage.detail(img_id, admin=True)["status"], "queued")
            app._resume_media_tasks(queued)
            self.assertEqual(worker.call_count, 2)
            self.assertEqual(reopened.get_task(img_id)["status"], "completed")
            self.assertEqual(reopened.get_task(vid_id)["status"], "completed")
            self.assertEqual(reopened.get_task(cancelled_id)["status"], "cancelled")
            self.assertTrue(reopened.get_task(sent_id)["needs_reconcile"])
            self.assertEqual(reopened.get_task(sent_id)["status"], "interrupted")
            self.assertEqual(reopened.get_task(private_id)["error_stage"], "before_send")
            self.assertFalse(reopened.get_task(private_id)["needs_reconcile"])
            self.assertEqual(app.client_usage.detail(img_id, admin=True)["status"], "completed")
            self.assertEqual(app.client_usage.detail(private_id, admin=True)["charge"], 0)
            reused = json.loads(app.create_image_task(image, "restart-image", _=retained).body)
            self.assertEqual(reused["id"], img_id)
            self.assertEqual(worker.call_count, 2)
            self.assertFalse(list(app.PENDING_MEDIA.directory.iterdir()))

    def test_expired_base64_result_reports_gone(self):
        req = app.ImageRequest(prompt="fixture", response_format="b64_json")
        result = {"filename": "already-pruned.png", "kind": "image", "size": 10}
        with self.assertRaises(app.HTTPException) as error:
            app._image_response(req, result)
        self.assertEqual(error.exception.status_code, 410)

if __name__=="__main__":unittest.main(verbosity=2)
