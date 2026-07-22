# -*- coding: utf-8 -*-
"""Claude Code가 켜질 때 위젯을 함께 띄운다 (SessionStart 훅에서 호출).

이미 떠 있으면 아무것도 하지 않는다. install.ps1이 이 파일을 ~/.claude 에
복사하면서 아래 WIDGET 경로를 실제 설치 경로로 바꿔 넣는다.
수동 설치라면 WIDGET 을 ClaudeUsageWidget.pyw 의 절대 경로로 직접 고치면 된다.
"""
import os
import socket
import subprocess
import sys

WIDGET = r"__WIDGET_PATH__"
PORT = 53917


def already_running():
    try:
        c = socket.create_connection(("127.0.0.1", PORT), timeout=1)
        c.close()
        return True
    except OSError:
        return False


def pythonw():
    exe = sys.executable
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if os.path.exists(cand) else exe


if not already_running() and os.path.exists(WIDGET):
    DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen([pythonw(), WIDGET], creationflags=DETACHED,
                         close_fds=True, cwd=os.path.dirname(WIDGET))
    except OSError:
        pass
