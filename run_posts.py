"""Original search-card scraper, with industry URLs and assignment columns."""

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

import yaml
from scrapers.linkedin_posts_scraper import (
    AccessBlocked, login_linkedin, scrape_linkedin_posts, scrape_linkedin_comments,
)
from post_outputs import prepare_posts, empty_coverage, export_collection

ROOT = Path(__file__).resolve().parent


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--industry', choices=['manufacturing', 'education', 'all'], default='manufacturing')
    parser.add_argument('--login', action='store_true', help='Open LinkedIn for manual login and save the local session')
    parser.add_argument('--max-candidates', type=int, default=120, help='Examined-candidate limit, 1–120 (default: 120)')
    args = parser.parse_args()
    return args


async def main(args):
    if args.login:
        await login_linkedin()
        return
    with (ROOT / 'inputs_posts.yaml').open(encoding='utf-8') as stream:
        config = yaml.safe_load(stream)
    for entry in config:
        if args.industry != 'all' and entry['industry'] != args.industry:
            continue
        entry = {**entry, 'candidate_limit': args.max_candidates}
        all_posts, filters, comments, comment_cache = [], {}, [], {}
        reason = 'searches_finished'
        interrupted = False

        def retain_candidate(row):
            all_posts.append(row)
            return len(prepare_posts(all_posts, entry)[0]) >= 80

        try:
            for keyword in entry['keywords']:
                report = {}
                try:
                    found = await scrape_linkedin_posts(
                        keyword=keyword, time_filter='past-month', content_type='posts',
                        sort_by='recency', max_posts=min(50, args.max_candidates-len(all_posts)),
                        batch=entry['batch'], id_prefix=entry['id_prefix'],
                        report=report, on_post=retain_candidate, comment_cache=comment_cache,
                    )
                finally:
                    filters[keyword] = report.get('date_filter', 'not_observed')
                retained, _, _ = prepare_posts(all_posts, entry)
                print(f"Found {len(found)} candidates for '{keyword}'; {len(retained)} importable in total", flush=True)
                if len(retained) >= 80:
                    reason = 'target_reached'
                    break
                if len(all_posts) >= args.max_candidates:
                    reason = 'candidate_limit_reached'
                    break
                await asyncio.sleep(1)
        except AccessBlocked as exc:
            reason = str(exc)
            print(f'Collection stopped: {reason}. Complete the human step before rerunning.', flush=True)
        except (KeyboardInterrupt, asyncio.CancelledError):
            reason, interrupted = 'interrupted', True
        except Exception as exc:
            reason = f'collection_error:{type(exc).__name__}'
            print(reason, flush=True)
        posts, rejected, stats = prepare_posts(all_posts, entry)
        coverage = empty_coverage(posts)
        if reason in {'target_reached', 'candidate_limit_reached', 'searches_finished'}:
            try:
                await scrape_linkedin_comments(posts, comments, coverage, comment_cache)
            except AccessBlocked as exc:
                reason = str(exc)
            except (KeyboardInterrupt, asyncio.CancelledError):
                reason, interrupted = 'interrupted', True
            except Exception as exc:
                reason = f'comment_collection_error:{type(exc).__name__}'
        stamp = datetime.now().astimezone().strftime('%Y_%m_%d_%H_%M_%S_%f')
        output = ROOT / 'results' / f'{entry["id_prefix"]}_{stamp}'
        summary = export_collection(output, all_posts, entry, posts, rejected, stats, comments, coverage, reason, filters)
        print(f"Saved to {output}\n{len(all_posts)} candidates; {len(posts)} importable posts; {len(comments)} comments.", flush=True)
        if summary['retained_with_relative_date']:
            print(f"Publication dates derived from relative labels: {summary['retained_with_relative_date']}.", flush=True)
        if not summary['requirements_met']:
            print(f'Missing fields: exact dates={summary["missing_exact_dates"]}, likes={summary["missing_like_counts"]}, comments={summary["missing_comment_counts"]}.', flush=True)
            print(f'Candidate text and URLs: {output / (entry["id_prefix"] + "_posts_candidates.csv")}', flush=True)
            print('Assignment sample incomplete. See collection_summary.json and rejected_candidates.json.', flush=True)
        if interrupted:
            raise KeyboardInterrupt
        if reason not in {'target_reached', 'candidate_limit_reached', 'searches_finished'}:
            break


if __name__ == '__main__':
    try:
        args = arguments()
        from browser_runtime import relay_windows_chrome
        windows_result = relay_windows_chrome(sys.argv[1:])
        if windows_result is not None:
            sys.exit(windows_result)
        asyncio.run(main(args))
    except KeyboardInterrupt:
        pass
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
