"""
pronews_poster.py
로컬 Mac 전용: Supabase(draft) → Tistory 비공개 저장
대상 블로그: dawney.tistory.com (pronews.kr)
실행하면 Chromium 창이 뜨고, 카카오 로그인 버튼만 클릭하면 자동으로 완성됩니다.
"""

import os
import json
import asyncio
import logging
import requests
from datetime import datetime
from typing import Optional
from dotenv import load_dotenv
from supabase import create_client
from playwright.async_api import async_playwright, Page

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
BLOG_NAME = os.getenv("PRONEWS_BLOG_NAME", "dawney")
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
TABLE_NAME = "pronews_articles"
AUTH_FILE = "auth_pronews.json"


class ProNewsPoster:
    def __init__(self):
        self.supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        self.blog_name = BLOG_NAME

    # -------------------------------------------------------------------------
    # Supabase에서 draft 기사 가져오기
    # -------------------------------------------------------------------------
    def get_draft_articles(self, limit: int = 5):
        try:
            res = self.supabase.table(TABLE_NAME) \
                .select('*') \
                .eq('status', 'draft') \
                .order('created_at', desc=False) \
                .limit(limit) \
                .execute()
            return res.data or []
        except Exception as e:
            logger.error(f"Supabase 조회 실패: {e}")
            return []

    def update_status(self, row_id: str, tistory_url: str = ''):
        try:
            self.supabase.table(TABLE_NAME) \
                .update({
                    'status': 'private',
                    'tistory_url': tistory_url,
                }) \
                .eq('id', row_id) \
                .execute()
            logger.info(f"✅ 상태 업데이트: private")
        except Exception as e:
            logger.error(f"상태 업데이트 실패: {e}")

    # -------------------------------------------------------------------------
    # 대표이미지 설정
    # -------------------------------------------------------------------------
    async def _set_thumbnail(self, page: Page, img_url: str):
        try:
            for sel in [
                'button:has-text("대표이미지")',
                'button.btn-thumbnail',
                'label:has-text("대표이미지")',
            ]:
                btn = page.locator(sel).first
                if await btn.count() > 0:
                    await btn.click()
                    await asyncio.sleep(1)
                    for input_sel in ['input[placeholder*="URL"]', 'input[placeholder*="url"]']:
                        inp = page.locator(input_sel).first
                        if await inp.count() > 0:
                            await inp.fill(img_url)
                            await inp.press('Enter')
                            await asyncio.sleep(1)
                            break
                    for confirm_sel in ['button:has-text("확인")', 'button:has-text("저장")', 'button:has-text("적용")']:
                        confirm = page.locator(confirm_sel).first
                        if await confirm.count() > 0:
                            await confirm.click()
                            await asyncio.sleep(1)
                            logger.info("✓ 대표이미지 설정 완료")
                            break
                    return
            logger.warning("⚠️ 대표이미지 버튼 없음 → 스킵")
        except Exception as e:
            logger.warning(f"대표이미지 설정 실패 (스킵): {e}")

    # -------------------------------------------------------------------------
    # Tistory 글쓰기 (비공개)
    # -------------------------------------------------------------------------
    async def write_to_tistory(self, page: Page, article: dict) -> bool:
        try:
            write_url = f"https://{self.blog_name}.tistory.com/manage/post"
            await page.goto(write_url, wait_until='networkidle', timeout=30000)
            await asyncio.sleep(5)
            await page.keyboard.press("Escape")
            await asyncio.sleep(1)

            # 로그인 확인
            if 'login' in page.url or 'auth' in page.url:
                logger.error("❌ 로그인이 필요합니다. 브라우저에서 로그인 후 Enter를 눌러주세요.")
                input("로그인 완료 후 Enter...")
                await page.goto(write_url, wait_until='networkidle', timeout=30000)
                await asyncio.sleep(5)

            # 제목 입력
            title_field = page.locator('#post-title-inp').first
            if await title_field.count() > 0:
                await title_field.click()
                await title_field.fill(article['title'])
                logger.info("✓ 제목 입력 완료")
            else:
                logger.error("❌ 제목 필드 없음")
                return False

            await asyncio.sleep(1)

            # 이미지 포함 본문 구성
            content_html = article['content_html']
            final_img_url = article.get('img_url', '')

            if final_img_url:
                img_tag = (
                    '<p style="text-align:center;">'
                    f'<img src="{final_img_url}" '
                    'style="max-width:100%; height:auto; display:block; margin:0 auto;">'
                    '</p>\n'
                )
                content_html = img_tag + content_html

            # 본문 입력 (iframe)
            editor_found = False
            frame = page.frame(name="editor-tistory_ifr")
            if frame:
                editable = frame.locator('[contenteditable="true"]').first
                if await editable.count() > 0:
                    await editable.click()
                    await editable.evaluate(
                        '(node, html) => { node.innerHTML = html; node.dispatchEvent(new Event("input", {bubbles: true})); }',
                        content_html
                    )
                    editor_found = True
                    logger.info("✓ 본문 입력 완료")

            if not editor_found:
                for f in page.frames:
                    if 'editor' in f.name.lower():
                        editable = f.locator('[contenteditable="true"]').first
                        if await editable.count() > 0:
                            await editable.click()
                            await editable.evaluate(
                                '(node, html) => { node.innerHTML = html; node.dispatchEvent(new Event("input", {bubbles: true})); }',
                                content_html
                            )
                            editor_found = True
                            logger.info(f"✓ 본문 입력 완료 ({f.name})")
                            break

            if not editor_found:
                logger.error("❌ 에디터 없음")
                return False

            await asyncio.sleep(2)

            # 대표이미지 설정
            if final_img_url:
                await self._set_thumbnail(page, final_img_url)

            # 발행 버튼 → 비공개 → 발행
            if await page.locator('#publish-layer-btn').count() > 0:
                await page.click('#publish-layer-btn')
                await asyncio.sleep(2)

                try:
                    priv_label = page.locator('label:has-text("비공개")').first
                    if await priv_label.count() > 0:
                        await priv_label.dispatch_event("click")
                        logger.info("✓ 비공개 설정")
                except Exception:
                    pass

                for s in ['button:has-text("발행")', 'button.btn_publish', '#publish-btn']:
                    if await page.locator(s).count() > 0:
                        await page.click(s)
                        logger.info("✓ 발행 클릭")
                        break
            else:
                logger.error("❌ 발행 버튼 없음")
                return False

            await page.wait_for_load_state('networkidle', timeout=15000)
            await asyncio.sleep(3)
            logger.info(f"✨ 완료: {page.url}")
            return True

        except Exception as e:
            logger.error(f"❌ 글쓰기 실패: {e}")
            return False

    # -------------------------------------------------------------------------
    # 메인 실행
    # -------------------------------------------------------------------------
    async def run(self):
        articles = self.get_draft_articles(limit=5)

        if not articles:
            logger.info("처리할 기사 없음 (Supabase draft 없음)")
            return

        logger.info(f"처리할 기사: {len(articles)}개")

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=False,
                args=['--disable-blink-features=AutomationControlled']
            )

            if os.path.exists(AUTH_FILE):
                with open(AUTH_FILE, "r") as f:
                    auth_data = json.load(f)
                context = await browser.new_context()
                await context.add_cookies(auth_data.get("cookies", []))
                logger.info(f"✓ {AUTH_FILE} 로드 완료")
            else:
                context = await browser.new_context()
                logger.info(f"{AUTH_FILE} 없음 → 브라우저에서 직접 로그인 필요")

            posted = 0
            for article in articles:
                logger.info(f"▶ {article['title'][:50]}")
                page = await context.new_page()
                success = await self.write_to_tistory(page, article)
                tistory_url = page.url if success else ''
                await page.close()

                if success:
                    self.update_status(article['id'], tistory_url)
                    posted += 1

                await asyncio.sleep(3)

            # 세션 저장
            storage = await context.storage_state()
            with open(AUTH_FILE, "w") as f:
                json.dump(storage, f, indent=2)
            logger.info(f"✓ {AUTH_FILE} 갱신 완료")

            await browser.close()

        logger.info(f"완료: {posted}개 Tistory 비공개 저장")
        logger.info("Tistory 관리자에서 비공개 글 확인 후 공개 클릭하세요.")


if __name__ == "__main__":
    asyncio.run(ProNewsPoster().run())
