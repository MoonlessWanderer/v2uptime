"""Actual browser checks for history refresh, stale errors and small screens."""
import pytest

pytest.importorskip('playwright.sync_api')
from tests.test_server_table import panel


def test_history_refresh_and_stale_failure(panel):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        hits = []
        def history(route):
            data = route.fetch().json()
            hits.append(route.request.url)
            data['summary']['avg_latency'] = len(hits) * 100
            route.fulfill(json=data)
        page.route('**/api/servers/*/history?*', history)
        page.goto(panel)
        card = page.locator('.card').first
        card.locator('.card-name').click()
        page.wait_for_function("() => document.querySelector('.card [data-stat=ping]').textContent === '100 ms'")
        card.locator('.card-name').click()
        with page.expect_response('**/partials/live?*'):
            page.evaluate("""() => {
              Object.defineProperty(document,'hidden',{configurable:true,value:true});
              document.dispatchEvent(new Event('visibilitychange'));
              Object.defineProperty(document,'hidden',{configurable:true,value:false});
              document.dispatchEvent(new Event('visibilitychange'));
            }""")
        page.wait_for_timeout(100)
        card.locator('.card-name').click()
        page.wait_for_function("() => document.querySelector('.card [data-stat=ping]').textContent === '200 ms'")
        assert len(hits) == 2
        page.evaluate("""() => {
          const real=window.fetch;
          window.fetch=(url,opts)=>String(url).includes('range=7d') ?
            new Promise((resolve,reject)=>{window.rejectOld=()=>reject(Error('old request failed'));}) : real(url,opts);
        }""")
        card.locator('[data-range="7d"]').click()
        card.locator('[data-range="24h"]').click()
        page.wait_for_function("() => document.querySelector('.card [data-stat=ping]').textContent === '300 ms'")
        page.evaluate('window.rejectOld()')
        page.wait_for_timeout(100)
        assert card.locator('[data-stat=ping]').inner_text() == '300 ms'
        browser.close()


def test_admin_header_keeps_signout_inside_small_viewport(panel):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width':320,'height':800})
        page.goto(panel+'/admin/login')
        page.fill('#username','admin')
        page.fill('#password','correct-horse')
        page.click('button[type=submit]')
        page.wait_for_url('**/admin/')
        for width in (320,390,1280):
            page.set_viewport_size({'width':width,'height':800})
            page.evaluate('document.fonts.ready')
            page.wait_for_timeout(250)  # Wait for the responsive navigation transition.
            box=page.locator('[aria-label="Sign out"]').bounding_box()
            assert box['x'] >= 0 and box['x']+box['width'] <= width
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), page.evaluate("""() => [...document.querySelectorAll('main *')].filter(e=>e.getBoundingClientRect().right>innerWidth+1).map(e=>[e.tagName,e.className,e.textContent.slice(0,120)])""")
        browser.close()
