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


class VersionReportingTests(unittest.TestCase):
    def test_version_check_never_upgrades_or_masks_version(self):
        app._UPDATE_CACHE['ts']=0
        def get(url,**kwargs):
            body=({'status':'ahead'} if '/compare/' in url else {'version':'1.6.10'}) if 'raw.githubusercontent' in url or '/compare/' in url else [{'sha':'ab' * 20,'commit':{'message':'fix','committer':{'date':'2026-11-13'}}}]
            return SimpleNamespace(raise_for_status=lambda:None,json=lambda:body)
        with patch.object(app,'_read_local_version',return_value={'version':'1.6.9'}),patch.dict(os.environ,{'MUSE2API_BUILD_SHA':'1234567' + '0' * 33}),patch('requests.get',side_effect=get),patch.object(app,'_upgrade_from_github_sync') as upgrade:
            result=app._check_update_sync(force=True)
        self.assertEqual(result['local_version'],'1.6.9');self.assertEqual(result['remote_version'],'1.6.10')
        self.assertEqual(result['local_sha'],'1234567');self.assertTrue(result['has_update']);self.assertFalse(result['up_to_date'])
        upgrade.assert_not_called()
        with patch('requests.get',side_effect=ConnectionError('offline')):
            result=app._check_update_sync(force=True)
        self.assertIsNone(result['up_to_date']);self.assertTrue(result['check_error'])

    def test_same_older_diverged_or_unconfirmed_do_not_offer_upgrade(self):
        local = "a" * 40
        remote = "b" * 40
        for relation, answer in [("identical", True), ("behind", True),
                                 ("diverged", False), ("unknown", None)]:
            with self.subTest(relation=relation):
                def get(url, **kwargs):
                    if "raw.githubusercontent" in url:
                        body = {"version": "1.6.10"}
                    else:
                        body = [{"sha": remote, "commit": {"message": "fixture"}}]
                    return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)

                with patch.dict(os.environ, {"MUSE2API_BUILD_SHA": "", "RENDER_GIT_COMMIT": local}), \
                     patch.object(app, "_git", return_value=SimpleNamespace(returncode=0, stdout="")), \
                     patch("requests.get", side_effect=get), \
                     patch.object(app, "_compare_update_commits", return_value=relation) as compare:
                    result = app._check_update_sync(force=True)
                self.assertEqual(compare.call_args.args[:2], (local, remote))
                self.assertFalse(result["has_update"])
                self.assertEqual(result["up_to_date"], answer)
                with patch.object(app, "MANAGED_DEPLOYMENT", False), \
                     patch.object(app, "_check_update_sync", return_value=result), \
                     patch.object(app, "_ensure_git_repo") as ensure:
                    with self.assertRaises(app.HTTPException) as cm:
                        app._upgrade_from_github_sync()
                self.assertEqual(cm.exception.status_code, 409)
                ensure.assert_not_called()

    def test_actual_fork_is_ahead_of_upstream(self):
        local = "eeadd690b3a260ff75ba9219ad494006c3702b09"
        remote = "19db32a3d6ef50dc87567a8ee0e35dd50c8f7837"
        with patch.object(app, "_git", return_value=SimpleNamespace(returncode=0, stdout=remote)), \
             patch("requests.get") as get:
            self.assertEqual(app._compare_update_commits(local, remote, True, {}), "behind")
        get.assert_not_called()

    def test_managed_upgrade_is_blocked(self):
        with patch.object(app,'MANAGED_DEPLOYMENT',True),patch('requests.get') as get:
            with self.assertRaises(app.HTTPException) as cm:app._upgrade_from_github_sync()
        self.assertEqual(cm.exception.status_code,409);get.assert_not_called()

    def test_failed_upgrade_does_not_record_remote_as_installed(self):
        target='a'*40
        marker=Path(HOME.name)/'failed-installed-sha'
        broken=SimpleNamespace(check_returncode=lambda:(_ for _ in ()).throw(RuntimeError('git failed')))
        app.store.tasks={}
        with patch.object(app,'MANAGED_DEPLOYMENT',False),patch.object(app,'_check_update_sync',return_value={'remote_commit':target,'remote_relation':'ahead','has_update':True}), \
             patch.object(app,'_ensure_git_repo'),patch.object(app,'_git',return_value=broken), \
             patch.object(app,'_installed_sha_file',return_value=str(marker)),patch('requests.get',return_value=SimpleNamespace(status_code=503)):
            with self.assertRaises(app.HTTPException) as cm:app._upgrade_from_github_sync()
        self.assertEqual(cm.exception.status_code,502)
        self.assertFalse(marker.exists())


if __name__=="__main__":unittest.main(verbosity=2)
