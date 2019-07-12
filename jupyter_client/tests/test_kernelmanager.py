"""Tests for the KernelManager"""

# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.


import json
import os
pjoin = os.path.join
import signal
from subprocess import PIPE
import sys
import time
import threading
import multiprocessing as mp
import pytest
from unittest import TestCase

from traitlets.config.loader import Config
from jupyter_core import paths
from jupyter_client import KernelManager
from ..manager import start_new_kernel
from .utils import test_env, skip_win32

TIMEOUT = 30

class TestKernelManager(TestCase):
    def setUp(self):
        self.env_patch = test_env()
        self.env_patch.start()
    
    def tearDown(self):
        self.env_patch.stop()

    def _install_test_kernel(self):
        kernel_dir = pjoin(paths.jupyter_data_dir(), 'kernels', 'signaltest')
        os.makedirs(kernel_dir)
        with open(pjoin(kernel_dir, 'kernel.json'), 'w') as f:
            f.write(json.dumps({
                'argv': [sys.executable,
                         '-m', 'jupyter_client.tests.signalkernel',
                         '-f', '{connection_file}'],
                'display_name': "Signal Test Kernel",
            }))

    def _get_tcp_km(self):
        c = Config()
        km = KernelManager(config=c)
        return km

    def _get_ipc_km(self):
        c = Config()
        c.KernelManager.transport = 'ipc'
        c.KernelManager.ip = 'test'
        km = KernelManager(config=c)
        return km

    def _run_lifecycle(self, km):
        km.start_kernel(stdout=PIPE, stderr=PIPE)
        self.assertTrue(km.is_alive())
        km.restart_kernel(now=True)
        self.assertTrue(km.is_alive())
        km.interrupt_kernel()
        self.assertTrue(isinstance(km, KernelManager))
        km.shutdown_kernel(now=True)

    def test_tcp_lifecycle(self):
        km = self._get_tcp_km()
        self._run_lifecycle(km)

    @skip_win32
    def test_ipc_lifecycle(self):
        km = self._get_ipc_km()
        self._run_lifecycle(km)

    def test_get_connect_info(self):
        km = self._get_tcp_km()
        cinfo = km.get_connection_info()
        keys = sorted(cinfo.keys())
        expected = sorted([
            'ip', 'transport',
            'hb_port', 'shell_port', 'stdin_port', 'iopub_port', 'control_port',
            'key', 'signature_scheme',
        ])
        self.assertEqual(keys, expected)

    @skip_win32
    def test_signal_kernel_subprocesses(self):
        self._install_test_kernel()
        km, kc = start_new_kernel(kernel_name='signaltest')
        def execute(cmd):
            kc.execute(cmd)
            reply = kc.get_shell_msg(TIMEOUT)
            content = reply['content']
            self.assertEqual(content['status'], 'ok')
            return content
        
        self.addCleanup(kc.stop_channels)
        self.addCleanup(km.shutdown_kernel)
        N = 5
        for i in range(N):
            execute("start")
        time.sleep(1) # make sure subprocs stay up
        reply = execute('check')
        self.assertEqual(reply['user_expressions']['poll'], [None] * N)
        
        # start a job on the kernel to be interrupted
        kc.execute('sleep')
        time.sleep(1) # ensure sleep message has been handled before we interrupt
        km.interrupt_kernel()
        reply = kc.get_shell_msg(TIMEOUT)
        content = reply['content']
        self.assertEqual(content['status'], 'ok')
        self.assertEqual(content['user_expressions']['interrupted'], True)
        # wait up to 5s for subprocesses to handle signal
        for i in range(50):
            reply = execute('check')
            if reply['user_expressions']['poll'] != [-signal.SIGINT] * N:
                time.sleep(0.1)
            else:
                break
        # verify that subprocesses were interrupted
        self.assertEqual(reply['user_expressions']['poll'], [-signal.SIGINT] * N)

    def test_start_new_kernel(self):
        self._install_test_kernel()
        km, kc = start_new_kernel(kernel_name='signaltest')
        self.addCleanup(kc.stop_channels)
        self.addCleanup(km.shutdown_kernel)

        self.assertTrue(km.is_alive())
        self.assertTrue(kc.is_alive())


class TestParallel:

    @pytest.fixture(autouse=True)
    def env(self):
        env_patch = test_env()
        env_patch.start()
        yield
        env_patch.stop()

    @pytest.fixture(params=['tcp', 'ipc'])
    def transport(self, request):
        return request.param

    @pytest.fixture
    def config(self, transport):
        c = Config()
        c.transport = transport
        if transport == 'ipc':
            c.ip = 'test'
        return c

    def _install_test_kernel(self):
        kernel_dir = pjoin(paths.jupyter_data_dir(), 'kernels', 'signaltest')
        os.makedirs(kernel_dir)
        with open(pjoin(kernel_dir, 'kernel.json'), 'w') as f:
            f.write(json.dumps({
                'argv': [sys.executable,
                         '-m', 'jupyter_client.tests.signalkernel',
                         '-f', '{connection_file}'],
                'display_name': "Signal Test Kernel",
            }))

    def test_start_sequence_kernels(self, config):
        """Ensure that a sequence of kernel startups doesn't break anything."""

        self._install_test_kernel()
        self._run_signaltest_lifecycle(config)
        self._run_signaltest_lifecycle(config)
        self._run_signaltest_lifecycle(config)

    def test_start_parallel_thread_kernels(self, config):
        self._install_test_kernel()
        self._run_signaltest_lifecycle(config)

        thread = threading.Thread(target=self._run_signaltest_lifecycle, args=(config,))
        thread2 = threading.Thread(target=self._run_signaltest_lifecycle, args=(config,))
        try:
            thread.start()
            thread2.start()
        finally:
            thread.join()
            thread2.join()

    def test_start_parallel_process_kernels(self, config):
        self._install_test_kernel()

        self._run_signaltest_lifecycle(config)
        thread = threading.Thread(target=self._run_signaltest_lifecycle, args=(config,))
        proc = mp.Process(target=self._run_signaltest_lifecycle, args=(config,))
        try:
            thread.start()
            proc.start()
        finally:
            thread.join()
            proc.join()

        assert proc.exitcode == 0

    def test_start_sequence_process_kernels(self, config):
        self._install_test_kernel()
        self._run_signaltest_lifecycle(config)
        proc = mp.Process(target=self._run_signaltest_lifecycle, args=(config,))
        try:
            proc.start()
        finally:
            proc.join()

        assert proc.exitcode == 0
    
    def _prepare_kernel(self, km, startup_timeout=TIMEOUT, **kwargs):
        km.start_kernel(**kwargs)
        kc = km.client()
        kc.start_channels()
        try:
            kc.wait_for_ready(timeout=startup_timeout)
        except RuntimeError:
            kc.stop_channels()
            km.shutdown_kernel()
            raise

        return kc

    def _run_signaltest_lifecycle(self, config=None):
        km = KernelManager(config=config, kernel_name='signaltest')
        kc = self._prepare_kernel(km, stdout=PIPE, stderr=PIPE)

        def execute(cmd):
            kc.execute(cmd)
            reply = kc.get_shell_msg(TIMEOUT)
            content = reply['content']
            assert content['status'] == 'ok'
            return content

        execute("start")
        assert km.is_alive()
        execute('check')
        assert km.is_alive()

        km.restart_kernel(now=True)
        assert km.is_alive()
        execute('check')

        km.shutdown_kernel()
