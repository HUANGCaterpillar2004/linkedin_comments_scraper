import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from post_outputs import (
    COMMENT_COLUMNS, COVERAGE_COLUMNS, canonical_url, empty_coverage,
    export_collection, past_month, prepare_posts,
)
from scrapers.linkedin_posts_scraper import POST_COLUMNS, count_value, date_value, page_published_date

ENTRY = dict(id_prefix='MFG', batch='IND_MFG_IND', keywords=['manufacturing hiring'])


def candidate(number=1, **changes):
    row = dict(url=f'https://www.linkedin.com/posts/example-share-{number}-abc?tracking=1',
               text=f'We are hiring a production engineer for our manufacturing plant, position {number}. Apply to join our assembly team.',
               publishedAt='2026-11-18', collectedAt='2026-11-19T12:00:00+08:00',
               likes=0, commentsCount=2, keyword='manufacturing hiring', _public=True, _full_text=True)
    return {**row, **changes}


class OutputTests(unittest.TestCase):
    def test_missing_abbreviated_counts_and_explicit_zero(self):
        for label in ['', 'Like', '评论', '1.2K', '2万', '-1', '1.2']:
            self.assertEqual(count_value(label), '')
        for label, expected in [('0', 0), ('2 条评论', 2), ('1,234', 1234), ('1 234 reactions', 1234)]:
            self.assertEqual(count_value(label), expected)
        posts, rejected, _ = prepare_posts([candidate(commentsCount='')], ENTRY)
        self.assertEqual(posts, [])
        self.assertIn('missing_comment_count', rejected[0]['reasons'])

    def test_tracking_and_all_permalink_kinds(self):
        for kind in ['activity', 'share', 'ugcPost']:
            self.assertEqual(canonical_url(f'https://www.linkedin.com/posts/example-{kind}-123-ab?x=1#x'),
                             f'https://www.linkedin.com/posts/example-{kind}-123-ab/')
        self.assertEqual(canonical_url('https://www.linkedin.com/jobs/view/123'), '')
        self.assertEqual(canonical_url('https://linkedin.com.evil.test/posts/a-share-123-x'), '')

    def test_dedup_by_urn_and_normalized_text(self):
        raw = [candidate(), candidate(text='  WE ARE HIRING A DIFFERENT PRODUCTION ENGINEER. '),
               candidate(2, text=candidate()['text'].upper())]
        posts, _, stats = prepare_posts(raw, ENTRY)
        self.assertEqual(len(posts), 1)
        self.assertEqual(stats['duplicate'], 2)
        self.assertEqual(posts[0]['likes'], 0)
        self.assertEqual(posts[0]['ageDays'], '')
        self.assertEqual(posts[0]['analysisEligible'], 'true')

    def test_relative_calendar_date_and_summary(self):
        from scrapers.linkedin_posts_scraper import date_from_relative
        observed = '2024-03-31T02:30:00+08:00'
        for label, expected in [('4 小时 •', '2024-03-30'), ('1 天 • 已编辑 •', '2024-03-30'),
                                ('1mo', '2024-02-29'), ('2 weeks ago', '2024-03-17'),
                                ('20 min', '2024-03-31'), ('unknown', '')]:
            self.assertEqual(date_from_relative(label, observed), expected)
        self.assertEqual(date_from_relative('1 天', '2024-03-31'), '')
        raw = [candidate(ageSource='relative')]
        posts, rejected, stats = prepare_posts(raw, ENTRY)
        self.assertEqual(posts[0]['ageSource'], 'relative')
        with tempfile.TemporaryDirectory() as folder:
            summary = export_collection(Path(folder), raw, ENTRY, posts, rejected, stats,
                                        [], empty_coverage(posts), 'searches_finished', {})
        self.assertEqual(summary['importable_posts'], 1)
        self.assertEqual(summary['retained_with_relative_date'], 1)
        self.assertEqual(summary['posts_with_text_exact_date_and_counts'], 0)
        self.assertEqual(summary['missing_exact_dates'], 1)

    def test_exact_dates_and_calendar_window(self):
        self.assertEqual(date_value('1 week ago'), '')
        self.assertEqual(date_value('2026年11月18日'), '2026-11-18')
        self.assertEqual(date_value('2026-02-30'), '')
        self.assertTrue(past_month('2026-02-28', '2026-03-31T00:00:00+08:00'))
        self.assertFalse(past_month('2026-11-20', '2026-11-19T00:00:00+08:00'))
        self.assertFalse(past_month('2026-11-18', '2026-11-19T00:00:00'))
        self.assertEqual(prepare_posts([candidate(publishedAt='1w')], ENTRY)[0], [])

    def test_public_and_complete_text_evidence(self):
        for changes, reason in [({'_public':False}, 'public_visibility_not_verified'),
                                ({'_full_text':False}, 'incomplete_text'),
                                ({'text':'I am looking for a job as a manufacturing engineer. I have years of production experience.'}, 'not_a_hiring_advertisement')]:
            posts, rejected, _ = prepare_posts([candidate(**changes)], ENTRY)
            self.assertFalse(posts)
            self.assertIn(reason, rejected[0]['reasons'])

    def test_target_cap_and_comment_inspection_limit(self):
        posts, _, _ = prepare_posts([candidate(i) for i in range(1, 100)], ENTRY)
        self.assertEqual(len(posts), 80)
        coverage = empty_coverage(posts)
        self.assertEqual(sum(r['coverage_status']=='comments_not_loaded' for r in coverage), 30)
        self.assertEqual(sum(r['coverage_status']=='not_inspected_due_limit' for r in coverage), 50)

    def test_all_outputs_empty_and_formula_safe(self):
        row = candidate(text='=We are hiring production engineers for a manufacturing plant. Contact Jane Doe at person@example.com or +1 555 123 4567. https://example.com/apply')
        posts, rejected, stats = prepare_posts([row], ENTRY)
        self.assertEqual(len(posts), 1)
        self.assertNotIn('Jane Doe', posts[0]['text'])
        self.assertNotIn('person@example.com', posts[0]['text'])
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            summary = export_collection(output, [row], ENTRY, posts, rejected, stats, [], empty_coverage(posts), 'searches_finished', {})
            self.assertFalse(summary['requirements_met'])
            for suffix, columns in [('posts_import',POST_COLUMNS),('comments_import',COMMENT_COLUMNS),('comment_coverage',COVERAGE_COLUMNS)]:
                with (output/f'MFG_{suffix}.csv').open(encoding='utf-8', newline='') as stream:
                    reader = csv.DictReader(stream)
                    self.assertEqual(tuple(reader.fieldnames), columns)
                    if suffix=='posts_import':
                        self.assertTrue(next(reader)['text'].startswith("'="))
            export_collection(output, [], ENTRY, [], [], {}, [], [], 'login_required', {})
            self.assertEqual(json.loads((output/'MFG_collection_summary.json').read_text())['importable_posts'], 0)

    def test_comment_foreign_key_validation(self):
        posts, rejected, stats = prepare_posts([candidate()], ENTRY)
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                export_collection(Path(folder), [], ENTRY, posts, rejected, stats,
                                  [{'postId':'MFG9999','post_url':posts[0]['url']}], empty_coverage(posts), 'searches_finished', {})


class MetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_comment_date_cannot_become_post_date(self):
        script = AsyncMock()
        script.text_content.return_value = json.dumps([
            {'@type':'Comment', '@id':'urn:li:share:123', 'datePublished':'2026-11-19'},
            {'@type':'SocialMediaPosting', '@id':'urn:li:share:456', 'datePublished':'2026-11-19'},
            {'@type':'SocialMediaPosting', '@id':'urn:li:share:123', 'datePublished':'2026-11-18'},
        ])
        page = AsyncMock()
        page.query_selector_all.return_value = [script]
        self.assertEqual(await page_published_date(page,'123'), '2026-11-18')
        self.assertEqual(await page_published_date(page,'789'), '')


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def run_collection(self, limit, interrupt=False, target=False):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import patch
        import run_posts
        entry = {**ENTRY, 'industry':'manufacturing', 'keywords':['manufacturing hiring','factory hiring']}
        calls = []
        sequence = 0

        async def scrape(**kwargs):
            nonlocal sequence
            calls.append(kwargs['max_posts'])
            rows = []
            for _ in range(kwargs['max_posts']):
                sequence += 1
                row = candidate(sequence, publishedAt='2026-11-18' if target else '')
                rows.append(row)
                stop = kwargs['on_post'](row)
                if interrupt:
                    raise asyncio.CancelledError
                if stop:
                    break
            kwargs['report']['date_filter'] = 'Past month'
            return rows

        async def comments(posts, rows, coverage, cache):
            for record in coverage[:30]:
                record['coverage_status'] = 'no_visible_comments'

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'inputs_posts.yaml').write_text(json.dumps([entry]), encoding='utf-8')
            with patch.object(run_posts,'ROOT',root), patch.object(run_posts,'scrape_linkedin_posts',scrape), \
                 patch.object(run_posts,'scrape_linkedin_comments',comments), patch.object(run_posts.asyncio,'sleep',AsyncMock()), \
                 patch('builtins.print'):
                args = SimpleNamespace(login=False, industry='manufacturing', max_candidates=limit)
                if interrupt:
                    with self.assertRaises(KeyboardInterrupt):
                        await run_posts.main(args)
                else:
                    await run_posts.main(args)
            output = next((root/'results').iterdir())
            summary = json.loads((output/'MFG_collection_summary.json').read_text())
            self.assertTrue((output/'MFG_comments_import.csv').exists())
            self.assertTrue((output/'MFG_comment_coverage.csv').exists())
            self.assertTrue((output/'MFG_rejected_candidates.json').exists())
            return summary, calls

    async def test_candidate_limit_applies_across_queries(self):
        summary, calls = await self.run_collection(120)
        self.assertEqual(calls, [50,50])  # Available configured queries exhausted at 100.
        self.assertEqual(summary['raw_candidates_examined'],100)
        self.assertEqual(summary['importable_posts'],0)
        self.assertEqual(summary['missing_exact_dates'],100)
        summary, calls = await self.run_collection(60)
        self.assertEqual(calls,[50,10])
        self.assertEqual(summary['raw_candidates_examined'],60)
        self.assertEqual(summary['candidate_limit'],60)
        self.assertEqual(summary['stopped_reason'],'candidate_limit_reached')

    async def test_interruption_preserves_observed_candidates(self):
        summary, calls = await self.run_collection(6,interrupt=True)
        self.assertEqual(summary['raw_candidates_examined'],1)
        self.assertEqual(summary['stopped_reason'],'interrupted')
        self.assertFalse(summary['requirements_met'])

    async def test_stops_at_80_and_exports_coverage(self):
        summary, calls = await self.run_collection(120,target=True)
        self.assertEqual(summary['raw_candidates_examined'],80)
        self.assertEqual(summary['importable_posts'],80)
        self.assertEqual(summary['posts_without_visible_comments'],30)
        self.assertEqual(summary['posts_not_inspected_due_limit'],50)
        self.assertTrue(summary['requirements_met'])


if __name__ == '__main__':
    unittest.main()
