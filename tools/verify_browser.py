"""Optional synthetic browser verification; requires Playwright and Chromium."""
import os
import argparse
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from playwright.sync_api import sync_playwright

parser=argparse.ArgumentParser()
parser.add_argument('--screenshot')
args=parser.parse_args()

with tempfile.TemporaryDirectory() as private:
    db=str(Path(private)/'demo.sqlite')
    subprocess.run(['python','-m','review','--db',db,'demo'],check=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    process=subprocess.Popen(['python','-m','review','--db',db,'serve','--port',str(port)],stdout=subprocess.DEVNULL)
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=os.environ.get('CHROMIUM','/usr/bin/chromium'),headless=True,args=['--no-sandbox'])
            page=browser.new_page(viewport={'width':1280,'height':1000});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            for attempt in range(30):
                try:page.goto(f'http://127.0.0.1:{port}');break
                except Exception:time.sleep(0.1)
            page.get_by_placeholder('输入合约名称').fill('missing')
            page.get_by_text('暂无交易，或筛选无结果。历史计划：未知。').wait_for()
            page.get_by_placeholder('输入合约名称').fill('DEMO')
            page.get_by_role('button',name='展开单笔回放').first.click()
            page.get_by_text('展开成交来源').first.click()
            page.get_by_label('修订内容').fill('合成验收：事后修订')
            page.get_by_role('button',name='保存新版本').click()
            page.get_by_text('合成验收：事后修订',exact=False).wait_for()
            page.reload();page.get_by_text('合成验收：事后修订',exact=False).wait_for()
            page.get_by_text('合成演示数据 · 无真实账户',exact=True).wait_for()
            if args.screenshot:
                page.screenshot(path=args.screenshot,full_page=True)
            page.get_by_text('展开成交来源').first.click()
            assert page.locator('img').count()==0
            assert page.evaluate('window.__sourceXss') is None
            attack='<img src=x onerror=window.__noteXss=1></script><script>window.__noteXss=1</script>'
            page.get_by_label('修订内容').fill(attack)
            page.get_by_role('button',name='保存新版本').click()
            page.get_by_text(attack,exact=False).wait_for()
            assert page.locator('img, script:not([src])').count()==0
            assert page.evaluate('window.__noteXss') is None
            page.get_by_role('button',name='返回交易列表').click()
            page.get_by_role('button',name='展开单笔回放').first.wait_for()
            page.goto(f'http://127.0.0.1:{port}/#%broken');page.get_by_role('button',name='展开单笔回放').first.wait_for()
            assert not errors,errors
            browser.close();print('PASS: synthetic filter, detail, source, note version, refresh, back, source/note XSS escaping, malformed hash; no page errors')
    finally:process.terminate();process.wait(timeout=10)
