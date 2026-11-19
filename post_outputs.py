"""Assignment CSV mapping and reporting; no browser/session logic here."""

import calendar
import csv
import json
import re
import unicodedata
from collections import Counter
from datetime import date, datetime
from urllib.parse import unquote, urlsplit

from scrapers.linkedin_posts_scraper import POST_COLUMNS, count_value, source_identifier

COMMENT_COLUMNS = ('id','sourceId','postId','post_url','text','batch','collectedAt','provenance','reviewStatus','textEmpty')
COVERAGE_COLUMNS = ('post_id','post_url','reported_comments_count','visible_comments_captured','coverage_status')
ROLE_PATTERNS = (
    (r'\b(?:recruiters?|human resources|talent acquisition|hr manager)\b', 'Recruiting and HR'),
    (r'\b(?:product manager|project manager|program manager)\b', 'Product and project management'),
    (r'\b(?:designers?|ux|ui)\b', 'Design'),
    (r'\b(?:engineers?|technicians?|developers?|mechanics?|electricians?|cnc)\b', 'Engineering and technology'),
    (r'\b(?:marketing|sales|business development)\b', 'Marketing and sales'),
    (r'\b(?:nurses?|physicians?|therapists?|clinical)\b', 'Healthcare'),
    (r'\b(?:chefs?|hotel|hospitality|waiters?)\b', 'Hospitality'),
    (r'\b(?:operators?|administrators?|coordinators?|supervisors?|operations|plant manager|factory manager)\b', 'Operations and administration'),
)
INDUSTRY_NAMES = {'MFG':'Manufacturing and industrial production','EDU':'Education and training'}


def redact(text):
    text = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[email hidden]', text or '')
    text = re.sub(r'https?://[^\s<>]+|www\.[^\s<>]+', '[link hidden]', text, flags=re.I)
    text = re.sub(r'(?i)(\b(?:phone|mobile|whatsapp|tel|call)\s*:?\s*)\+?\d[\d ().-]{5,}\d', lambda m:m[1]+'[phone hidden]', text)
    text = re.sub(r'(?<!\w)\+\d{1,3}(?:[ ().-]?\d){7,12}(?!\w)', '[phone hidden]', text)
    name = r'[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2}'
    text = re.sub(r'(\b(?i:contact|ask|email|message|regards|sincerely)[,:]?\s+)'+name,lambda m:m[1]+'[name hidden]',text)
    text = re.sub(r'^'+name+r'(?=\s+is hiring\b)','[name hidden]',text)
    return ' '.join(text.split())


def text_key(text):
    return ' '.join(unicodedata.normalize('NFKC',text).casefold().split())


def canonical_url(url):
    parsed = urlsplit(url or '')
    if parsed.scheme != 'https' or not re.fullmatch(r'(?:[a-z]{2,3}\.)?linkedin\.com',parsed.hostname or ''):
        return ''
    path=unquote(parsed.path)
    match=re.fullmatch(r'/feed/update/urn:li:(activity|share|ugcPost):(\d+)/?',path)
    if not match and path.startswith('/posts/'):
        match=re.search(r'-(activity|share|ugcPost)-(\d+)(?:-|/|$)',path)
    return 'https://www.linkedin.com'+path.rstrip('/')+'/' if match else ''


def past_month(published,collected):
    try:
        observed=datetime.fromisoformat(collected)
        if observed.tzinfo is None:return False
        today=observed.date()
        y,m=(today.year-1,12) if today.month==1 else (today.year,today.month-1)
        cutoff=date(y,m,min(today.day,calendar.monthrange(y,m)[1]))
        return cutoff<=date.fromisoformat(published)<=today
    except (ValueError,TypeError):return False


def prepare_posts(raw,entry):
    posts,rejected=[],[]
    urls,urns,texts=set(),set(),set()
    stats=Counter()
    prefix,batch=entry['id_prefix'],entry['batch']
    for number,original in enumerate(raw,1):
        row={column:original.get(column,'') for column in POST_COLUMNS}
        row['url']=canonical_url(row['url'])
        row['sourceId']=source_identifier(row['url'])
        row['text']=redact(row['text'])
        for field in ('likes','commentsCount','reposts'):
            row[field]=count_value(str(row[field]))
        reasons=[]
        if not row['url']:reasons.append('missing_post_url')
        try:exact=bool(row['publishedAt']) and date.fromisoformat(row['publishedAt']).isoformat()==row['publishedAt']
        except (ValueError,TypeError):exact=False
        if not exact:reasons.append('missing_exact_date')
        elif not past_month(row['publishedAt'],row['collectedAt']):reasons.append('outside_past_month')
        if row['likes']=='':reasons.append('missing_like_count')
        if row['commentsCount']=='':reasons.append('missing_comment_count')
        if len(row['text'])<40:reasons.append('text_under_40_characters')
        if original.get('_full_text') is False:reasons.append('incomplete_text')
        if original.get('_public') is not True:reasons.append('public_visibility_not_verified')
        text=row['text']
        if len(re.findall(r'\b[A-Za-z]{2,}\b',text))<6:reasons.append('english_text_not_verified')
        if not re.search(r'\b(?:hiring|recruiting|vacanc(?:y|ies)|job openings?|open positions?|join our team|looking for|seeking)\b',text,re.I):reasons.append('no_clear_hiring_opportunity')
        if re.search(r"\b(?:open to work|i am looking for a job|i'm looking for a job|career advice|interview tips)\b",text,re.I):reasons.append('not_a_hiring_advertisement')
        context=(r'\b(?:manufactur\w*|factor(?:y|ies)|production|industrial|plant|machin\w*|cnc|assembly|maintenance)\b' if prefix=='MFG'
                 else r'\b(?:education\w*|schools?|colleges?|universit\w*|tutor\w*|teach\w*|instructors?|lecturers?|curriculum|edtech|training coordinator)\b')
        if not re.search(context,text,re.I):reasons.append('industry_context_not_verified')
        key=text_key(text)
        duplicate=(bool(row['url']) and row['url'] in urls) or (bool(row['sourceId']) and row['sourceId'] in urns) or (bool(key) and key in texts)
        if duplicate:reasons.append('duplicate')
        if len(posts)>=80:reasons.append('not_retained_due_target')
        for reason in reasons:stats[reason]+=1
        if reasons:
            rejected.append({'candidate':number,'keyword':row['keyword'],'url':row['url'],'reasons':reasons})
            continue
        row.update(id=f'{prefix}{len(posts)+1:04d}',batch=batch,ageDays='',isDuplicate='false',
                   analysisEligible='true' if len(text)>=80 else 'false',reviewStatus='draft',ageSource=row['ageSource'] or 'exact',
                   provenance=f'AI-assisted public LinkedIn collection; industry={batch}')
        row['jobFamily']=next((family for pattern,family in ROLE_PATTERNS if re.search(pattern,text,re.I)),'Other or unclassified')
        posts.append(row)
        urls.add(row['url']);urns.add(row['sourceId']);texts.add(key)
    return posts,rejected,stats


def empty_coverage(posts):
    return [dict(zip(COVERAGE_COLUMNS,[post['id'],post['url'],post['commentsCount'],0,
                 'comments_not_loaded' if index<30 else 'not_inspected_due_limit'])) for index,post in enumerate(posts)]


def cell(value):
    if isinstance(value,bool):return 'true' if value else 'false'
    if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@')):return "'"+value
    return value


def write_csv(path,columns,rows):
    with path.open('w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=columns)
        writer.writeheader()
        writer.writerows({column:cell(row.get(column,'')) for column in columns} for row in rows)


def export_collection(output,raw,entry,posts,rejected,stats,comments,coverage,reason,filters):
    prefix=entry['id_prefix']
    stats=Counter(stats)
    if len(coverage)!=len(posts) or {r['post_id'] for r in coverage}!={r['id'] for r in posts}:
        raise ValueError('Coverage must contain exactly one row for every retained post')
    lookup={post['id']:post for post in posts}
    actual_counts=Counter(comment['postId'] for comment in comments)
    for row in coverage:
        if row['visible_comments_captured']!=actual_counts[row['post_id']]:raise ValueError('Comment coverage count mismatch')
    for comment in comments:
        if comment['postId'] not in lookup or comment['post_url']!=lookup[comment['postId']]['url']:raise ValueError('Comment refers to a different post')
    output.mkdir(parents=True,exist_ok=True)
    write_csv(output/f'{prefix}_posts_import.csv',POST_COLUMNS,posts)
    write_csv(output/f'{prefix}_comments_import.csv',COMMENT_COLUMNS,comments)
    write_csv(output/f'{prefix}_comment_coverage.csv',COVERAGE_COLUMNS,coverage)
    candidates=[{**row,'text':redact(row.get('text',''))} for row in raw]
    write_csv(output/f'{prefix}_posts_candidates.csv', (*POST_COLUMNS, 'publishedLabel'), candidates)
    statuses=Counter(row['coverage_status'] for row in coverage)
    exact_posts = sum(row['ageSource'] == 'exact' for row in posts)
    complete=(len(posts)>=80 and exact_posts == len(posts) and all(row['coverage_status']!='comments_not_loaded' for row in coverage[:30])
              and reason in {'target_reached','candidate_limit_reached','searches_finished'})
    summary={
        'collection_date':datetime.now().astimezone().date().isoformat(),
        'industry_name':INDUSTRY_NAMES[prefix],'industry_code':entry['batch'],
        'search_queries':entry['keywords'],'actual_date_filter':filters,
        'raw_candidates_examined':len(raw),'unique_posts':len(posts),'importable_posts':len(posts),
        'candidate_limit':entry.get('candidate_limit',120),'target_posts':80,
        'comment_rows':len(comments),'posts_with_comments':statuses['comments_captured'],
        'posts_without_visible_comments':statuses['no_visible_comments'],
        'missing_post_urls':stats['missing_post_url'],'missing_exact_dates':sum(not row.get('publishedAt') or row.get('ageSource') == 'relative' for row in raw),
        'missing_like_counts':stats['missing_like_count'],'missing_comment_counts':stats['missing_comment_count'],
        'duplicates_removed':stats['duplicate'],'stopped_reason':reason,
        'analysis_eligible_posts':sum(row['analysisEligible']=='true' for row in posts),
        'posts_comments_not_loaded':statuses['comments_not_loaded'],
        'posts_not_inspected_due_limit':statuses['not_inspected_due_limit'],
        'requirements_met':complete,'target_reached':len(posts)>=80,
        'collection_status':'ready_for_human_review' if complete else 'incomplete',
        'minimum_sample_met':len(posts)>=60,
        'posts_with_text_exact_date_and_counts':exact_posts,
        'retained_with_relative_date':len(posts) - exact_posts,
        'rejection_counts':dict(stats),'human_review_required':True,
        'review_notes':'Drafts require final industry, hiring-intent and personal-name review.',
        'privacy_review':'Recognized contacts and linked names redacted; remaining names require human review.',
        'formula_escape':"An apostrophe prefixes cells starting with =, +, - or @.",
        'reminder':'Counts are visible engagement rather than hiring effectiveness.',
    }
    (output/f'{prefix}_collection_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    (output/f'{prefix}_rejected_candidates.json').write_text(json.dumps(rejected,ensure_ascii=False,indent=2),encoding='utf-8')
    return summary
