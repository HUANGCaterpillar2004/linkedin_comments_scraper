"""Regression fixture for SDUI sibling comments and nested replies; no network."""
import unittest

from browser_runtime import chrome_executable
from scrapers.linkedin_posts_scraper import read_comment_section
from playwright.async_api import async_playwright

class CommentDOMTests(unittest.IsolatedAsyncioTestCase):
    async def test_sibling_comments_are_bound_to_one_post(self):
        try:
            executable = chrome_executable()         
        except (FileNotFoundError, ImportError):
            self.skipTest('Installed native Chrome and Playwright required')
        comments = ''.join(
            f'<div data-view-name="comment-container" style="display:contents">'
            f'<a data-view-name="comment-actor-description">Private Author {i}</a>'
            f'<p data-view-name="comment-commentary">Visible top-level comment {i}; email contact@example.com</p>'
            + ('<div data-view-name="comment-container"><p data-view-name="comment-commentary">Nested reply excluded</p></div>' if i == 0 else '')
            + '<button data-view-name="comment-reply">Reply</button></div>'
            for i in range(7)
        )
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=executable, headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(
                    '<div data-testid="lazy-column"><div role="listitem">'
                    '<div data-view-name="feed-full-update" style="display:contents"><p>Post one</p></div>'
                    f'<div data-view-name="comment-box"></div>{comments}</div>'
                    '<div role="listitem"><div data-view-name="feed-full-update">Post two</div>'
                    '<div data-view-name="comment-container"><p data-view-name="comment-commentary">Wrong post excluded</p></div>'
                    '</div></div>'
                )
                root = await page.query_selector('[data-view-name="feed-full-update"]')
                rows, status = await read_comment_section(root, page)
                self.assertEqual(status, 'comments_captured')
                self.assertEqual(len(rows), 5)
                text = ' '.join(row['text'] for row in rows)
                self.assertNotIn('Private Author', text)
                self.assertNotIn('Nested reply', text)
                self.assertNotIn('Wrong post', text)
                self.assertNotIn('contact@example.com', text)
                self.assertIn('[email hidden]', text)
            finally:
                await browser.close()

    async def test_search_card_before_hydration(self):
        from scrapers.linkedin_posts_scraper import POST_SELECTOR, capture_post
        try:
            executable = chrome_executable()
            from playwright.async_api import async_playwright
        except (FileNotFoundError, ImportError):
            self.skipTest('Installed native Chrome and Playwright required')
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=executable, headless=True)
            try:
                page = await browser.new_page()
                await page.set_content("""
                    <div role="listitem" componentkey="update-card-focus-example">
                      <p componentkey="actor-time">1 天 • 已编辑 •</p>
                      <div componentkey="translatable-commentary-FeTranslationUrn(contentUrnCommentUrn=null, contentUrnShareUrn=ContentUrnShareUrn(shareUrn=ShareUrn(shareId=123)))">
                        <p><span data-testid="expandable-text-box">We are hiring production engineers.</span></p>
                      </div>
                      <a href="/jobs/view/456/?track=abc">Job details</a>
                      <button aria-label="回应按钮状态: 无回应">43</button>
                      <button aria-label="评论">8</button>
                      <button aria-label="重新发布"></button>
                    </div>""")
                card = await page.query_selector(POST_SELECTOR)
                row = await capture_post(card, page, 'manufacturing hiring', 'IND_MFG_IND', 'MFG', check_detail=False)
                self.assertEqual(row['url'], 'https://www.linkedin.com/feed/update/urn:li:share:123/')
                self.assertEqual(row['relatedJobUrl'], 'https://www.linkedin.com/jobs/view/456/')
                self.assertEqual(row['text'], 'We are hiring production engineers.')
                self.assertEqual((row['likes'], row['commentsCount'], row['reposts']), (43, 8, ''))
                self.assertEqual((row['ageSource'], row['publishedLabel']), ('relative', '1 天 • 已编辑 •'))
                self.assertTrue(row['publishedAt'])
            finally:
                await browser.close()
