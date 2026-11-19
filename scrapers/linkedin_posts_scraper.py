import asyncio
from urllib.parse import quote, unquote, urljoin, urlsplit
from datetime import datetime, timedelta
from calendar import monthrange
from contextlib import asynccontextmanager
from pathlib import Path
import subprocess
import random
import re
import json
from browser_runtime import chrome_executable
from playwright.async_api import async_playwright
PROFILE_DIR = Path(__file__).resolve().parents[1] / 'linkedin_profile_chrome'


@asynccontextmanager
async def browser_session():
    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            str(PROFILE_DIR), channel='chrome', executable_path=chrome_executable(),
            headless=False, slow_mo=300,
        )
        try:
            yield browser
        finally:
            await browser.close()


async def login_linkedin():
    # Manual sign-in uses regular Chrome with no automation/debugging connection.
    print('将打开电脑上已安装的正式版Chrome。登录过程由您手动完成。')
    print('请登录 LinkedIn、完成验证，然后关闭本次打开的Chrome窗口。', flush=True)
    process = subprocess.Popen([
        chrome_executable(), f'--user-data-dir={PROFILE_DIR}',
        '--no-first-run', '--no-default-browser-check', 'https://www.linkedin.com/login',
    ])
    process.wait()
    print(f'浏览器资料保存在：{PROFILE_DIR}')
    print('确认已完成登录并关闭上述 Chrome 窗口后，再运行采集。')
    print('现在可以运行：python run_posts.py --industry manufacturing')

POST_COLUMNS = (
    'id', 'sourceId', 'url', 'relatedJobUrl', 'text', 'jobFamily', 'publishedAt',
    'collectedAt', 'ageDays', 'likes', 'commentsCount', 'reposts', 'batch', 'keyword',
    'isDuplicate', 'analysisEligible', 'provenance', 'reviewStatus', 'ageSource',
)

# Only post containers. A plain <li> also matches the site's policy/footer links.
POST_SELECTOR = (
    '.feed-shared-update-v2, .occludable-update, '
    '.search-result__occludable-update, .main-feed-activity-card, '
    '[data-view-name="feed-full-update"], [role="listitem"][componentkey^="update-card-focus"], '
    '[data-urn^="urn:li:activity:"], [data-urn^="urn:li:share:"], '
    '[data-urn^="urn:li:ugcPost:"]'
)
TEXT_SELECTOR = (
    'p[data-view-name="feed-commentary"], [componentkey^="translatable-commentary-"][componentkey*="contentUrnCommentUrn=null"] p, '
    '.update-components-text, .feed-shared-update-v2__description, '
    '.feed-shared-update-v2__commentary, .feed-shared-text__text-view, '
    '.feed-shared-inline-show-more-text .break-words, '
    '.attributed-text-segment-list__content, .search-result__snippets'
)


REACTIONS = ('[data-view-name="reaction-button"], button[aria-label^="回应按钮状态"], '
             '.social-details-social-counts__reactions-count, .social-details-social-counts__reaction-count')
COMMENTS = ('[data-view-name="feed-comment-button"], button[aria-label="评论"], '
            'button[aria-label="Comment" i], .social-details-social-counts__comments')
REPOSTS = ('[data-view-name="feed-share-button"], button[aria-label="重新发布"], '
           'button[aria-label="Repost" i], .social-details-social-counts__reposts')


async def commentary_permalink(body):
    if body is None:
        return ''
    key = await body.evaluate("""e => e.closest('[componentkey^="translatable-commentary-"]')
        ?.getAttribute('componentkey') || ''""")
    if 'contentUrnCommentUrn=null' not in key:
        return ''
    match = re.search(r'\b(share|ugcPost)Id=(\d+)\b', key)
    return f'https://www.linkedin.com/feed/update/urn:li:{match[1]}:{match[2]}/' if match else ''


def search_url(keyword, time_filter='past-month', geo_id=''):
    url = ('https://www.linkedin.com/search/results/content/'
           f'?keywords={quote(keyword, safe="")}&datePosted=%22{time_filter}%22')
    return url + f'&geoId={geo_id}' if geo_id else url


def source_identifier(url):
    match = re.search(r'(?:urn:li:(?:activity|share|ugcPost):|(?:activity|share|ugcPost)-)(\d+)', unquote(url))
    return match[1] if match else ''


def count_value(label):
    match = re.fullmatch(
        r'(\d{1,3}(?:[,\s]\d{3})+|\d+)(?:\s*(?:likes?|reactions?|comments?|reposts?|shares?|个?赞|条?评论|条?評論|次?转发|次?轉發))?',
        label.strip(), re.I,
    )
    return int(re.sub(r'[,\s]', '', match[1])) if match else ''


async def count_label(element):
    if element is None:
        return ''
    label = (await element.inner_text()).strip()
    if count_value(label) != '':
        return label
    for span in await element.query_selector_all('span'):
        value = (await span.inner_text()).strip()
        if count_value(value) != '':
            return value
    return label


def date_value(label):
    match = re.search(r'\b\d{4}-\d{2}-\d{2}(?=\b|T)', label)
    if match:
        try:
            return datetime.strptime(match[0], '%Y-%m-%d').date().isoformat()
        except ValueError:
            pass
    match = re.search(r'(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日', label)
    if match:
        try:
            return datetime(*map(int, match.groups())).date().isoformat()
        except ValueError:
            pass
    match = re.search(r'\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b', label)
    if match:
        value = ' '.join(match.groups())
        for format in ('%B %d %Y', '%b %d %Y'):
            try:
                return datetime.strptime(value, format).date().isoformat()
            except ValueError:
                pass
    return ''


async def page_published_date(page, source_id, response=None):
    """Read only the primary post's standard publication metadata."""
    scripts = []
    for script in await page.query_selector_all('script[type="application/ld+json"]'):
        scripts.append(await script.text_content() or 'null')
    if response is not None and not scripts:
        html = await response.text()
        scripts = re.findall(
            r'<script\b[^>]*type=[\"\']application/ld\+json[\"\'][^>]*>(.*?)</script>',
            html, re.I | re.S,
        )
    for script in scripts:
        try:
            data = json.loads(script)
        except (ValueError, TypeError):
            continue
        pending = data if isinstance(data, list) else [data]
        while pending:
            node = pending.pop(0)
            if not isinstance(node, dict):
                continue
            graph = node.get('@graph', [])
            pending.extend(graph if isinstance(graph, list) else [graph])
            kind = node.get('@type', [])
            if 'SocialMediaPosting' not in (kind if isinstance(kind, list) else [kind]):
                continue
            identities = [node.get('@id', ''), node.get('url', ''), node.get('mainEntityOfPage', '')]
            identities = [value.get('@id', value.get('url', '')) if isinstance(value, dict) else value for value in identities]
            if not source_id or not any(source_identifier(str(value)) == source_id for value in identities):
                continue
            try:
                value = datetime.fromisoformat(node['datePublished'].replace('Z', '+00:00'))
                return value.astimezone().date().isoformat() if value.tzinfo else value.date().isoformat()
            except (KeyError, TypeError, ValueError, AttributeError):
                pass
    return ''


async def copied_post_link(post, page):
    """The current UI exposes a normal Copy link menu instead of a permalink anchor."""
    menu = await post.query_selector('[data-view-name="feed-control-menu"]')
    if menu is None:
        return '', ''
    detail = None
    stage = 'open_menu'
    try:
        await page.bring_to_front()
        await page.context.grant_permissions(['clipboard-read', 'clipboard-write'], origin='https://www.linkedin.com')
        copied_notice = page.locator('[role="status"], [role="alert"]').filter(has_text=re.compile('复制|copied', re.I)).first
        try:
            await copied_notice.wait_for(state='hidden', timeout=5000)
        except Exception:
            pass
        await menu.click(timeout=5000)
        stage = 'copy_link'
        await page.locator('[data-view-name="feed-control-menu-copy-link"]').click(timeout=5000)
        stage = 'read_clipboard'
        await copied_notice.wait_for(state='visible', timeout=10000)
        link = (await page.evaluate('navigator.clipboard.readText()')).strip()
        parsed = urlsplit(link)
        if parsed.scheme != 'https' or parsed.hostname not in {'lnkd.in', 'www.linkedin.com', 'linkedin.com'}:
            return '', ''
        if parsed.hostname == 'lnkd.in':
            stage = 'resolve_short_link'
            detail = await page.context.new_page()
            response = await detail.goto(link, wait_until='domcontentloaded', timeout=30000)
            await check_access(detail, response)
            await detail.wait_for_url(
                re.compile(r'^https://(?:[a-z]{2,3}\.)?linkedin\.com/(?:feed/update|posts)/'),
                wait_until='domcontentloaded', timeout=15000,
            )
            link = detail.url
        parsed = urlsplit(link)
        if not re.fullmatch(r'(?:[a-z]{2,3}\.)?linkedin\.com', parsed.hostname or '') or not parsed.path.startswith(('/feed/update/', '/posts/')):
            return '', ''
        link = urljoin('https://www.linkedin.com', unquote(parsed.path))
        identifier = source_identifier(link)
        published = await page_published_date(detail, identifier, response=response) if detail and identifier else ''
        if detail and not published:
            detail_post = await detail.query_selector(POST_SELECTOR)
            if detail_post:
                published = await post_date(detail_post, detail)
        return link, published
    except AccessBlocked:
        raise
    except Exception as exc:
        print(f'Could not read this post link: {stage} ({type(exc).__name__}).')
        if detail is not None:
            target = urlsplit(detail.url)
            print(f'Post link destination: {target.hostname or "unknown"}/{target.path.strip("/").split("/")[0]}')
        return '', ''
    finally:
        if detail is not None:
            await detail.close()
        await page.keyboard.press('Escape')


def post_row(keyword, batch, text, url, likes='', comments='', reposts='', published='', related_job=''):
    """Map captured values to the requested columns; missing values stay empty."""
    likes, comments, reposts = (count_value(value) for value in (likes, comments, reposts))
    text = ' '.join(text.split())
    text = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[email hidden]', text)
    text = re.sub(r'(?i)(\b(?:phone|mobile|whatsapp|tel)\s*:?\s*)\+?\d[\d ().-]{5,}\d',
                  lambda found: found[1] + '[phone hidden]', text)
    return {
        'id': '', 'sourceId': source_identifier(url), 'url': url, 'relatedJobUrl': related_job,
        'text': text, 'jobFamily': 'Other or unclassified', 'publishedAt': published,
        'collectedAt': datetime.now().astimezone().isoformat(timespec='seconds'), 'ageDays': '',
        'likes': likes, 'commentsCount': comments, 'reposts': reposts, 'batch': batch, 'keyword': keyword,
        'isDuplicate': 'false',
        'analysisEligible': 'true' if len(text) >= 80 and likes != '' and comments != '' else 'false',
        'provenance': f'industry={batch}',
        'reviewStatus': 'draft', 'ageSource': 'exact' if published else '',
    }

async def human_like_scroll(page, times=3):
    # Wheel input must be over the feed, not the fixed navigation bar.
    body = await page.query_selector(TEXT_SELECTOR)
    if body:
        box = await body.bounding_box()
        if box:
            viewport = page.viewport_size
            await page.mouse.move(box['x'] + min(box['width'] / 2, 180),max(100, min(box['y'] + min(box['height'] / 2, 40), viewport['height'] - 60)),)
    for _ in range(times):
        await page.mouse.wheel(0, 2500)
        await asyncio.sleep(random.uniform(2, 4))

class AccessBlocked(RuntimeError):
    pass


async def check_access(page, response=None):
    path = urlsplit(page.url).path
    if re.match(r'^/(?:login|checkpoint|authwall|uas/login)(?:/|$)', path):
        raise AccessBlocked('login_or_verification_required')
    if response is not None and response.status in (401, 403, 429):
        raise AccessBlocked(f'access_blocked_http_{response.status}')
    challenge = page.locator('input[name="session_password"], iframe[src*="captcha"], #captcha-internal')
    if await challenge.count():
        raise AccessBlocked('login_or_verification_required')
    for notice in await page.query_selector_all('[role="alert"], .artdeco-inline-feedback--error'):
        label = await notice.inner_text()
        if re.search(r'too many requests|unusual activity|temporarily restricted|security verification|访问受限|安全验证|请求过多|账户.*限制', label, re.I):
            raise AccessBlocked('access_warning')


async def visible_text(element):
    text = await element.inner_text()
    for mention in await element.query_selector_all("a[href*='/in/'], a[href*='/pub/']"):
        name = (await mention.inner_text()).strip()
        if name:
            text = text.replace(name, '[name hidden]')
    return text


async def expand_text(post, page, selector=TEXT_SELECTOR):
    more = re.compile(r'^(?:(?:\.{3}|…)?\s*(?:see more|show more|more|展开|更多|查看更多))$', re.I)
    for control in await post.query_selector_all('button'):
        if more.fullmatch((await control.inner_text()).strip()) and await control.is_visible():
            await control.click(timeout=4000)
            await asyncio.sleep(0.5)
    element = await post.query_selector(selector)
    return element


# Only parse the age at the start of the actor header, before the audience/edit badge.
AGE_UNITS = {
    'seconds': '秒 秒钟 秒鐘 second seconds sec secs s',
    'minutes': '分钟 分鐘 分 minute minutes min mins m',
    'hours': '小时 小時 hour hours hr hrs h',
    'days': '天 日 day days d',
    'weeks': '周 週 星期 week weeks wk wks w',
    'months': '个月 個月 月 month months mo mos',
    'years': '年 year years yr yrs y',
}
AGE_UNIT = {alias: unit for unit, aliases in AGE_UNITS.items() for alias in aliases.split()}
AGE_PATTERN = re.compile(r'(\d{1,4})\s*(' + '|'.join(AGE_UNIT) + r')\s*(?:前|ago)?', re.I)


def age_token(label):
    return re.split(r'[•·]', label or '', maxsplit=1)[0].strip().lower()


def date_from_relative(label, collected_at):
    token = age_token(label)
    try:
        observed = datetime.fromisoformat(collected_at)
        if observed.tzinfo is None:
            return ''
        if token in ('刚刚', '剛剛', 'just now', 'today', '今天'):
            return observed.date().isoformat()
        if token in ('昨天', 'yesterday'):
            return (observed.date() - timedelta(days=1)).isoformat()
        match = AGE_PATTERN.fullmatch(token)
        if not match:
            return ''
        amount, unit = int(match[1]), AGE_UNIT[match[2]]
        if unit in ('months', 'years'):
            index = observed.year * 12 + observed.month - 1 - amount * (12 if unit == 'years' else 1)
            year, month_index = divmod(index, 12)
            month = month_index + 1
            day = min(observed.day, monthrange(year, month)[1])
            return observed.date().replace(year=year, month=month, day=day).isoformat()
        return (observed - timedelta(**{unit: amount})).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return ''


async def header_age_element(post):
    for header in await post.query_selector_all('p[componentkey]'):
        if await header.evaluate('(e, body) => !!e.closest(body)', TEXT_SELECTOR):
            continue
        token = age_token(await header.inner_text())
        if AGE_PATTERN.fullmatch(token) or token in ('刚刚', '剛剛', 'just now', 'today', '今天', '昨天', 'yesterday'):
            return header
    return None


async def post_date(post, page, header=None):
    timestamps = await post.query_selector_all(
        'time, [itemprop="datePublished"], a.update-components-actor__sub-description-link, '
        'a.feed-shared-actor__sub-description-link'
    )
    if header is not None:
        timestamps.append(header)
    for element in timestamps:
        if await element.evaluate("""(e, body) => !!e.closest(body + ', [data-view-name="comment-container"], '
            + '.comments-comment-item, .comments-comment-entity, a[href*="/jobs/view/"]')""", TEXT_SELECTOR):
            continue
        for label in [await element.inner_text(), await element.get_attribute('title') or '',
                      await element.get_attribute('datetime') or '', await element.get_attribute('content') or '',
                      await element.get_attribute('aria-label') or '']:
            exact = date_value(label)
            if exact:
                return exact
        try:
            await element.hover(timeout=2000)
            descriptions = (await element.get_attribute('aria-describedby') or '').split()
            for identifier in descriptions:
                label = await page.evaluate('id => document.getElementById(id)?.innerText || ""', identifier)
                exact = date_value(label)
                if exact:
                    return exact
        except Exception:
            pass
    return ''


async def capture_post(post, page, keyword, batch, id_prefix, comment_cache=None, *, check_detail=True):
    text_el = await expand_text(post, page)
    text = await visible_text(text_el) if text_el else ''
    likes = await count_label(await post.query_selector(REACTIONS))
    comments = await count_label(await post.query_selector(COMMENTS))
    shares = await count_label(await post.query_selector(REPOSTS))
    observed = datetime.now().astimezone().isoformat(timespec='seconds')
    header = await header_age_element(post)
    published_label = (await header.inner_text()).strip() if header else ''
    published = await post_date(post, page, header)
    link = ''
    for anchor in await post.query_selector_all("a[href*='/feed/update/'], a[href*='/posts/']"):
        if await anchor.evaluate("(e,body)=>!!e.closest(body)", TEXT_SELECTOR):
            continue  # An inline link can point to an entirely different post.
        candidate = urljoin('https://www.linkedin.com', await anchor.get_attribute('href') or '')
        parsed = urlsplit(candidate)
        if re.fullmatch(r'(?:[a-z]{2,3}\.)?linkedin\.com', parsed.hostname or '') and source_identifier(candidate):
            link = candidate.split('?')[0].split('#')[0]
            break
    if not link:
        urn = await post.get_attribute('data-urn') or await post.get_attribute('data-activity-urn') or ''
        if re.fullmatch(r'urn:li:(?:activity|share|ugcPost):\d+', urn):
            link = f'https://www.linkedin.com/feed/update/{urn}/'
    if not link:
        link = await commentary_permalink(text_el)
    if not link:
        link, metadata_date = await copied_post_link(post, page)
        published = published or metadata_date
    await check_access(page)
    job = await post.query_selector("a[href*='/jobs/view/']")
    related = urljoin('https://www.linkedin.com', (await job.get_attribute('href') or '').split('?')[0]) if job else ''
    if not re.fullmatch(r'https://(?:[a-z]{2,3}\.)?linkedin\.com/jobs/view/[^?#]+', related):
        related = ''
    row = post_row(keyword, batch, text, link, likes, comments, shares, published, related)
    row['collectedAt'] = observed
    row['publishedLabel'] = published_label
    # SDUI uses a globe with this explicit audience label; legacy versions use text.
    row['_public'] = await post.evaluate(r"""e=>Array.from(e.querySelectorAll('[aria-label],.update-components-actor__sub-description')).some(n=>/^(?:可见范围[:：]\s*全局|可見範圍[:：]\s*所有人|Visibility[:：]\s*(?:Anyone|Public)|Anyone|Public)$/i.test((n.getAttribute('aria-label')||n.innerText||'').trim()))""")
    row['_full_text'] = not bool(re.search(r'(?:see more|查看更多|展开全文)\s*$', text, re.I))
    if check_detail:
        await fill_from_detail(row, page)
        row['ageSource'] = 'exact' if row['publishedAt'] else ''
        row['analysisEligible'] = str(len(row['text']) >= 80 and row['likes'] != '' and row['commentsCount'] != '').lower()
    if not row['publishedAt']:
        row['publishedAt'] = date_from_relative(published_label, observed)
        if row['publishedAt']:
            row['ageSource'] = 'relative'
    if comment_cache is not None and row['sourceId'] not in comment_cache and len(comment_cache) < 30:
        from post_outputs import prepare_posts
        eligible, _, _ = prepare_posts([row], {'id_prefix': id_prefix, 'batch': batch})
        if eligible:
            # The search card already contains the exact post. Read its comment section here
            # because some SDUI share permalinks do not render a detail page in this session.
            try:
                comment_cache[row['sourceId']] = await read_comment_section(post, page)
            except AccessBlocked:
                raise
            except Exception:
                comment_cache[row['sourceId']] = ([], 'comments_not_loaded')
    return row


async def fill_from_detail(row, search_page):
    """Check the same post's normal detail page for evidence missing on its card."""
    identifier = row['sourceId']
    if not identifier or (row['publishedAt'] and row['likes'] != '' and row['commentsCount'] != ''):
        return
    detail = await search_page.context.new_page()
    try:
        response = await detail.goto(row['url'], wait_until='domcontentloaded', timeout=30000)
        await check_access(detail, response)
        if source_identifier(urlsplit(detail.url).path) != identifier:
            return
        if not row['publishedAt']:
            row['publishedAt'] = await page_published_date(detail, identifier, response)
            for meta in await detail.query_selector_all('head meta[property="article:published_time"], head meta[itemprop="datePublished"]'):
                row['publishedAt'] = row['publishedAt'] or date_value(await meta.get_attribute('content') or '')
        try:
            await detail.wait_for_selector(TEXT_SELECTOR, state='attached', timeout=5000)
        except Exception:
            await check_access(detail)
            return
        for card in await detail.query_selector_all(POST_SELECTOR):
            body = await card.query_selector(TEXT_SELECTOR)
            urn = await commentary_permalink(body) or await card.get_attribute('data-urn') or ''
            if source_identifier(urn) != identifier:
                continue
            row['publishedAt'] = row['publishedAt'] or await post_date(card, detail, await header_age_element(card))
            # Use one observation for the required pair of counts.
            likes = count_value(await count_label(await card.query_selector(REACTIONS)))
            comments = count_value(await count_label(await card.query_selector(COMMENTS)))
            if likes != '' and comments != '' and (row['likes'] == '' or row['commentsCount'] == ''):
                row.update(likes=likes, commentsCount=comments,
                           reposts=count_value(await count_label(await card.query_selector(REPOSTS))),
                           collectedAt=datetime.now().astimezone().isoformat(timespec='seconds'))
            break
    except AccessBlocked:
        raise
    except Exception as exc:
        await check_access(detail)
        print(f'Detail unavailable for {identifier}: {type(exc).__name__}', flush=True)
    finally:
        await detail.close()


async def scrape_linkedin_posts(keyword, geo_id='', time_filter='past-month', content_type='posts',
                                sort_by='recency', max_posts=50, batch='IND_MFG_IND', id_prefix='MFG',
                                report=None, on_post=None, comment_cache=None):
    results = []
    report = report if report is not None else {}
    seen = set()
    async with browser_session() as browser:
        page = browser.pages[0] if browser.pages else await browser.new_page()
        url = search_url(keyword, time_filter, geo_id)
        print(f'\nSearching Posts: {keyword}', flush=True)
        response = await page.goto(url, wait_until='domcontentloaded', timeout=60000)
        await asyncio.sleep(random.uniform(4, 6))
        await check_access(page, response)
        # Record the rendered selection, never assume the URL parameter was applied.
        filters = await page.locator('button,[role="button"]').all_inner_texts()
        report['date_filter'] = next((v.strip() for v in filters if re.fullmatch(r'Past month|过去一个月|过去 1 个月|上个月|過去一個月', v.strip(), re.I)), 'requested_past_month_not_confirmed')
        try:
            await page.wait_for_selector(POST_SELECTOR, state='attached', timeout=15000)
        except Exception:
            await check_access(page)
            report['stopped_reason'] = 'no_rendered_posts'
            return results
        # Read between scrolls: current LinkedIn virtualizes cards that leave the viewport.
        stale = 0
        for scroll in range(21):
            await check_access(page)
            before = len(results)
            for post in await page.query_selector_all(POST_SELECTOR):
                text_el = await post.query_selector(TEXT_SELECTOR)
                if not text_el:
                    continue
                key = await commentary_permalink(text_el) or ' '.join((await text_el.inner_text()).split())
                if not key or key in seen:
                    continue
                seen.add(key)
                try:
                    row = await capture_post(post, page, keyword, batch, id_prefix, comment_cache)
                except AccessBlocked:
                    raise
                except Exception as exc:
                    # Preserve a failed candidate for rejection reporting, not a fake successful row.
                    row = post_row(keyword, batch, await visible_text(text_el), '')
                    row['_capture_error'] = type(exc).__name__
                row['id'] = f'{id_prefix}{len(results)+1:04d}'
                results.append(row)
                target_reached = on_post(row) if on_post else False
                print(f'  Candidate {len(results)}: URL={bool(row["url"])} date={bool(row["publishedAt"])} likes={row["likes"]!s} comments={row["commentsCount"]!s}', flush=True)
                if target_reached:
                    report['stopped_reason'] = 'target_reached'
                    return results
                if len(results) >= max_posts:
                    report['stopped_reason'] = 'query_candidate_limit'
                    return results
            stale = stale + 1 if len(results) == before else 0
            if stale >= 3:
                report['stopped_reason'] = 'rendered_results_exhausted'
                break
            await human_like_scroll(page, times=1)
        else:
            report['stopped_reason'] = 'scroll_limit_reached'
    return results


async def read_comment_section(scope, page):
    from post_outputs import redact
    button = await scope.query_selector(COMMENTS + ', button.comment-button')
    if button:
        await button.click(timeout=5000)
        await asyncio.sleep(3)
    await check_access(page)
    # SDUI renders the comment list beside the display:contents post root.
    # Stay inside its one-post list item so comments from other results cannot leak in.
    if await scope.get_attribute('data-view-name') == 'feed-full-update':
        wrapper = (await scope.evaluate_handle("e=>e.closest('[role=listitem]')||e")).as_element()
        if wrapper and len(await wrapper.query_selector_all('[data-view-name="feed-full-update"]')) == 1:
            scope = wrapper
    roots = await scope.query_selector_all('[data-view-name="comment-container"], .comments-comment-item, .comments-comment-entity')
    items, seen = [], set()
    for root in roots:
        if await root.evaluate("e=>!!e.parentElement.closest('[data-view-name=comment-container],.comments-comment-item,.comments-comment-entity')"):
            continue
        element = await expand_text(root, page, 'p[data-view-name="comment-commentary"], .comments-comment-item__main-content, .comments-comment-item-content-body, .comments-comment-entity__content')
        if not element or not await element.is_visible():
            continue
        text = redact(await visible_text(element))
        if not text or text in seen or re.search(r'(?:see more|查看更多|展开全文)\s*$', text, re.I):
            continue
        seen.add(text)
        urn = await root.get_attribute('data-urn') or ''
        identifier = re.fullmatch(r'urn:li:comment:(\d+)', urn)
        items.append({'sourceId': identifier[1] if identifier else '', 'text': text,
                      'collectedAt': datetime.now().astimezone().isoformat(timespec='seconds')})
        if len(items) >= 5:
            break
    loaded = await scope.query_selector('[data-view-name="comment-box"], .comments-comments-list, .comments-comment-box')
    status = 'comments_captured' if items else ('no_visible_comments' if loaded else 'comments_not_loaded')
    return items, status


def append_comments(post, items, status, comments, coverage):
    for item in items[:5]:
        comments.append(dict(id=f'{post["id"][:3]}-C{len(comments)+1:04d}', sourceId=item['sourceId'],
            postId=post['id'], post_url=post['url'], text=item['text'], batch=post['batch'],
            collectedAt=item['collectedAt'], provenance=post['provenance'], reviewStatus='draft', textEmpty='false'))
    coverage['visible_comments_captured'] = min(len(items), 5)
    coverage['coverage_status'] = status


async def scrape_linkedin_comments(posts, comments, coverage, comment_cache=None):
    if not posts:
        return
    comment_cache = comment_cache or {}
    pending = []
    for post, status in zip(posts[:30], coverage[:30]):
        if post['sourceId'] in comment_cache and comment_cache[post['sourceId']][1] != 'comments_not_loaded':
            items, result = comment_cache[post['sourceId']]
            append_comments(post, items, result, comments, status)
        else:
            pending.append((post, status))
    if not pending:
        return
    async with browser_session() as browser:
        page = browser.pages[0] if browser.pages else await browser.new_page()
        for post, status in pending:
            response = await page.goto(post['url'], wait_until='domcontentloaded', timeout=45000)
            await asyncio.sleep(3)
            await check_access(page, response)
            try:
                await page.wait_for_selector('[data-view-name="feed-detail-page"], .feed-shared-update-v2', state='attached', timeout=15000)
                root = await page.query_selector(POST_SELECTOR)
                if root:
                    items, result = await read_comment_section(root, page)
                    append_comments(post, items, result, comments, status)
            except AccessBlocked:
                raise
            except Exception as exc:
                print(f'Comments not loaded for {post["id"]}: {type(exc).__name__}', flush=True)
            await asyncio.sleep(random.uniform(2, 4))
