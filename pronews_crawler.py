"""
pronews_crawler.py
GitHub Actions 전용: president.jp + blog.hinata-fortune.jp RSS 크롤링 → Gemini 번역 → Supabase 저장
대상 사이트: pronews.kr (dawney.tistory.com)
"""

import os
import json
import feedparser
import logging
import requests
import re
import time
from bs4 import BeautifulSoup
from datetime import datetime, date
from typing import Dict, Optional
from dotenv import load_dotenv
from supabase import create_client
import google.generativeai as genai

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

FEED_SOURCES = [
    "https://president.jp/list/rss",
    "https://blog.hinata-fortune.jp/feed/",
    "https://toyokeizai.net/list/feed/rss",       # 동양경제 (재테크/투자/자기계발)
    "https://www.lifehacker.jp/feed/index.xml",    # 라이프해커 일본 (생산성/자기계발)
]

KEYWORDS = [
    "運", "風水", "習慣", "成功", "お金", "資産", "金運",
    "仕事", "稼ぐ", "億", "節約", "投資", "富", "開運",
    "四柱推命", "占い", "運勢", "財", "豊か",
    "副業", "節税", "貯金", "FIRE", "自己啓発", "生産性",
    "ランニング", "習慣化", "メンタル", "健康", "朝活",
]

MAX_ARTICLES = 5
TABLE_NAME = "pronews_articles"
HISTORY_FILE = "posted_articles_pronews.json"

# -------------------------------------------------------------------------
# 저작권 문구 제거
# -------------------------------------------------------------------------
COPYRIGHT_PATTERNS = [
    r'<p[^>]*>©.*?</p>',
    r'<p[^>]*>&copy;.*?</p>',
    r'<p[^>]*>※.*?</p>',
    r'©[^\n<]*',
    r'&copy;[^\n<]*',
    r'ライター：[^\n<]*',
    r'掲載日：[^\n<]*',
]

def remove_copyright(html: str) -> str:
    for pattern in COPYRIGHT_PATTERNS:
        html = re.sub(pattern, '', html, flags=re.DOTALL)
    return html.strip()

def contains_keyword(title: str) -> bool:
    return any(kw in title for kw in KEYWORDS)

# 한국 독자와 무관한 일본 로컬 기사 제외
EXCLUDE_KEYWORDS = [
    # 왕실/정치
    "天皇", "皇室", "王室", "皇族", "御所", "宮内庁", "陛下", "殿下",
    "大河", "NHK", "参議院", "衆議院", "国会", "内閣", "首相",
    # 역사
    "戦国", "江戸", "明治", "昭和", "平成",
    # 국제/외교/군사
    "台湾", "北朝鮮", "中国", "ホルムズ", "海峡", "核", "ミサイル",
    "ウクライナ", "ロシア", "戦争", "軍", "防衛",
    # 재해/사건
    "地震", "津波", "災害", "事故", "殺", "犯罪",
    # 크루즈/감염병
    "クルーズ", "感染", "ウイルス",
]

def is_excluded(title: str) -> bool:
    return any(kw in title for kw in EXCLUDE_KEYWORDS)


class ProNewsCrawler:
    def __init__(self):
        self.supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        genai.configure(api_key=GEMINI_API_KEY)
        self.model = genai.GenerativeModel(GEMINI_MODEL)

        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                self.posted_articles = json.load(f)
        else:
            self.posted_articles = {}

    def is_in_supabase(self, url: str) -> bool:
        try:
            res = self.supabase.table(TABLE_NAME) \
                .select('id') \
                .eq('original_url', url) \
                .execute()
            return bool(res.data)
        except Exception:
            return False

    def collect_entries(self):
        feedparser.USER_AGENT = USER_AGENT
        new_entries = []
        fallback_keyword = []
        seen_titles = set()

        for url in FEED_SOURCES:
            feed = feedparser.parse(url)
            logger.info(f"[RSS] {url} → {len(feed.entries)}개")
            for e in feed.entries:
                # 제목 중복 체크
                title_key = e.title.strip()[:50]
                if title_key in seen_titles:
                    continue
                seen_titles.add(title_key)

                # 일본 로컬 기사 제외
                if is_excluded(e.title):
                    logger.info(f"제외 스킵: {e.title[:40]}")
                    continue

                # 키워드 통과한 기사만 수집
                if not contains_keyword(e.title):
                    continue

                if e.link not in self.posted_articles:
                    new_entries.append(e)
                else:
                    fallback_keyword.append(e)

        articles = new_entries[:MAX_ARTICLES]

        # 5개 미달 시: 키워드 통과 과거 기사로 보충 (Supabase 미저장만)
        if len(articles) < MAX_ARTICLES:
            needed = MAX_ARTICLES - len(articles)
            logger.info(f"새 기사 {len(articles)}개 → {needed}개 과거 기사로 보충")
            for e in fallback_keyword:
                if needed <= 0:
                    break
                if not self.is_in_supabase(e.link):
                    articles.append(e)
                    needed -= 1

        logger.info(f"최종 수집: {len(articles)}개")
        return articles

    def fetch_article(self, url: str) -> Optional[Dict]:
        try:
            headers = {'User-Agent': USER_AGENT}
            r = requests.get(url, headers=headers, timeout=10)
            r.encoding = 'utf-8'
            soup = BeautifulSoup(r.text, 'html.parser')

            og_img = soup.select_one('meta[property="og:image"]')
            img_url = og_img.get('content', '') if og_img else ''

            content = soup.select_one('article') or soup.select_one('.entry-content')
            if not content:
                return None

            if not img_url:
                first_img = content.select_one('img')
                if first_img:
                    img_url = first_img.get('src', '')

            return {'text': content.get_text()[:3000], 'img_url': img_url}
        except Exception as e:
            logger.error(f"기사 크롤링 실패: {e}")
            return None

    def translate_article(self, title: str, text: str) -> Optional[Dict]:
        prompt = (
            "다음 일본어 기사를 한국어 블로그 포스팅으로 충실하게 번역하세요.\n"
            "이 글은 재테크/자기계발 블로그의 원고 소재로 사용됩니다.\n\n"
            "【핵심 원칙】\n"
            "원문의 내용, 사실, 수치, 사례를 100% 빠짐없이 전달하는 것이 최우선입니다.\n"
            "오행, 풍수, 운세 관련 내용은 일절 추가하지 마세요.\n\n"
            "【통화 변환 규칙 (반드시 준수)】\n"
            "- 엔화(円/¥/万円)는 반드시 원화로 환산하여 표기할 것 (환율: 1엔 = 10원)\n"
            "  예) 1万円 → 10만 원, 100万円 → 1000만 원, 1億円 → 10억 원, 800億円 → 8000억 원\n"
            "- 달러($)는 그대로 달러로 표기할 것 (엔화로 환산 금지)\n"
            "- '~엔'이라는 표현은 본문에 절대 그대로 남기지 말 것\n\n"
            "아래 규칙을 반드시 지켜서 작성하세요:\n"
            "1. 원문의 모든 핵심 정보를 충실하게 전달할 것.\n"
            "2. '~다', '~이다' 체의 자연스러운 한국어로 작성할 것 (존댓말 사용 금지).\n"
            "3. 제목에 원문의 핵심 키워드를 포함할 것.\n"
            "4. 제목과 본문에 일본 기업명, 일본 고유명사, 일본 지명을 절대 포함하지 말 것.\n"
            "   (일본 기업명 → '한 글로벌 기업', 지명 → 생략 또는 일반화)\n"
            "5. 도입부 첫 2문장은 독자의 관심을 끄는 질문형 또는 공감형으로 작성할 것.\n"
            "6. h2 소제목을 2~3개 포함하여 글을 구조화할 것.\n"
            "7. 글자수 800자 이상으로 작성할 것.\n"
            "8. 글 마지막에 짧은 편집자 코멘트 1문장 추가할 것 (매번 다르게).\n"
            "9. 저자 이름, 저작권 표시(©, (C), ※), 출처 표기 모두 제거.\n"
            "10. img 태그는 절대 포함하지 말 것.\n\n"
            "반드시 아래 형식으로만 답하세요 (다른 설명 없이):\n"
            "[TITLE]한국어 제목 (한 줄, 태그 없이 텍스트만)\n"
            "[CONTENT]<p>도입부</p><h2>소제목</h2><p>본문 HTML 내용</p>\n\n"
            f"원문 제목: {title}\n"
            f"본문: {text}"
        )
        try:
            logger.info(f"Gemini 번역 중: {title[:40]}...")
            response = self.model.generate_content(prompt)
            raw = response.text

            t_match = re.search(r'\[TITLE\]\s*(.*?)\n', raw + '\n', re.IGNORECASE)
            c_match = re.search(r'\[CONTENT\]\s*(.*)', raw, re.DOTALL | re.IGNORECASE)

            t = t_match.group(1).strip() if t_match else title
            c = c_match.group(1).strip() if c_match else raw
            c = re.sub(r'```html|```', '', c).strip()
            c = re.sub(r'<img[^>]*/?>', '', c)
            c = remove_copyright(c)

            c, t = self.review_article(t, c)
            return {'title': t, 'content': c}
        except Exception as e:
            logger.error(f"❌ 번역 에러: {e}")
            return None

    def review_article(self, title: str, content: str):
        review_prompt = (
            "아래 한국어 블로그 글을 애드센스 수익화 관점에서 검토하고 부족한 부분만 보완하세요.\n"
            "체크 항목:\n"
            "- 제목과 본문에 일본 기업명/고유명사/지명이 있으면 제거하거나 일반화할 것\n"
            "- 제목에 검색 키워드가 포함되어 있는가\n"
            "- h2 소제목이 2개 이상인가\n"
            "- 글자수가 800자 이상인가\n"
            "- 도입부 첫 문장이 독자를 잡는 질문형/공감형인가\n"
            "부족한 부분만 보완해서 완성본을 반환하세요. 잘 된 부분은 그대로 두세요.\n\n"
            "반드시 아래 형식으로만 답하세요:\n"
            "[TITLE]제목\n"
            "[CONTENT]본문 HTML\n\n"
            f"[TITLE]{title}\n"
            f"[CONTENT]{content}"
        )
        try:
            logger.info("2차 검수 중... (7초 대기)")
            time.sleep(7)  # 분당 10회 한도 방지
            response = self.model.generate_content(review_prompt)
            raw = response.text

            t_match = re.search(r'\[TITLE\]\s*(.*?)\n', raw + '\n', re.IGNORECASE)
            c_match = re.search(r'\[CONTENT\]\s*(.*)', raw, re.DOTALL | re.IGNORECASE)

            t = t_match.group(1).strip() if t_match else title
            c = c_match.group(1).strip() if c_match else content
            c = re.sub(r'```html|```', '', c).strip()
            c = re.sub(r'<img[^>]*/?>', '', c)
            return c, t
        except Exception as e:
            logger.warning(f"⚠️ 2차 검수 실패 (원본 사용): {e}")
            return content, title

    def save_to_supabase(self, article_data: Dict) -> bool:
        try:
            res = self.supabase.table(TABLE_NAME) \
                .select('id') \
                .eq('original_url', article_data['link']) \
                .execute()
            if res.data:
                logger.info(f"이미 저장됨 (스킵): {article_data['link']}")
                return False

            self.supabase.table(TABLE_NAME).insert({
                'title':        article_data['title_kr'],
                'content_html': article_data['content_kr'],
                'original_url': article_data['link'],
                'img_url':      article_data['img_url'],
                'status':       'draft',
                'source':       article_data['source'],
                'created_at':   datetime.utcnow().isoformat(),
            }).execute()
            logger.info(f"✅ Supabase 저장: {article_data['title_kr'][:40]}")
            return True
        except Exception as e:
            logger.error(f"❌ Supabase 저장 실패: {e}")
            return False

    def run(self):
        logger.info("ProNews 크롤러 시작")
        entries = self.collect_entries()

        if not entries:
            logger.info("새로운 기사 없음")
            return

        saved = 0
        for entry in entries:
            logger.info(f"▶ {entry.title[:50]}")

            # 번역 전 Supabase 중복 체크 (토큰 낭비 방지)
            if self.is_in_supabase(entry.link):
                logger.info(f"이미 저장됨 (스킵): {entry.link}")
                continue

            data = self.fetch_article(entry.link)
            if not data:
                continue

            translated = self.translate_article(entry.title, data['text'])
            if not translated:
                continue

            source = "president" if "president.jp" in entry.link else "hinata"

            article_data = {
                'title_kr':   translated['title'],
                'content_kr': translated['content'],
                'link':       entry.link,
                'img_url':    data['img_url'],
                'source':     source,
            }

            if self.save_to_supabase(article_data):
                self.posted_articles[entry.link] = datetime.now().isoformat()
                with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
                    json.dump(self.posted_articles, f, ensure_ascii=False, indent=2)
                saved += 1

        logger.info(f"완료: {saved}개 저장")


if __name__ == "__main__":
    ProNewsCrawler().run()
