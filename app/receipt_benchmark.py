"""Offline A/B runner; no Telegram polling, database reads or writes.

Run with OPENAI_API_KEY in the environment:
python -m app.receipt_benchmark --manifest /private/cases.json --output /private/results
  --baseline-file /private/production_receipt_facts.py --model gpt-5.4

Manifest: [{"id":"sample", "files":["receipt.jpg"], "categories":["Food"]}].
Paths are relative to the manifest. Files may be photos or PDFs. Outputs include
private receipt contents: keep both manifests and outputs outside the repository.
Prices are USD per million tokens; supply prices for the chosen model explicitly.
Elapsed time excludes file reading/PDF rendering. Successful arithmetic is NOT a
quality score: manually compare names, duplicate purchases, discounts and categories.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import mimetypes
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from openai import AsyncOpenAI

from app import receipt_pipeline
from app.receipts import extract_pdf_text, render_pdf_pages


def summarize(records, prices):
    usage = [r['usage'] for r in records if r.get('usage')]
    inputs = sum(u['prompt_tokens'] for u in usage)
    cached = sum((u.get('prompt_tokens_details') or {}).get('cached_tokens', 0) for u in usage)
    outputs = sum(u['completion_tokens'] for u in usage)
    return {'calls': len(records), 'input_tokens': inputs, 'cached_tokens': cached,
            'output_tokens': outputs, 'usage_complete': all(r.get('usage') for r in records),
            'usd': ((inputs-cached)*prices[0]+cached*prices[1]+outputs*prices[2])/1e6}


async def measure(client, parser, model, images, categories, native_text, prices):
    records = []

    async def create(**kwargs):
        started = time.monotonic()
        record = {'stage': kwargs['response_format']['json_schema']['name'], 'usage': None}
        try:
            response = await client.chat.completions.create(**kwargs)
            record.update(usage=response.usage.model_dump() if response.usage else None,
                          output=response.choices[0].message.content)
            return response
        except (Exception, asyncio.CancelledError) as error:
            record['error'] = type(error).__name__
            raise
        finally:
            record['seconds'] = time.monotonic()-started
            records.append(record)

    proxy = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    started = time.monotonic()
    result, error = None, None
    try:
        parsed = await asyncio.wait_for(
            parser.recognize(proxy, model, images, categories, native_text), timeout=600)
        result = parsed.model_dump(mode='json')
    except Exception as exc:
        error = f'{type(exc).__name__}: {str(exc)[:300]}'
    return {'seconds': time.monotonic()-started, **summarize(records, prices),
            'result': result, 'error': error, 'records': records}


async def main(args):
    spec = importlib.util.spec_from_file_location('benchmark_baseline', args.baseline_file)
    baseline = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = baseline
    spec.loader.exec_module(baseline)
    cases = json.loads(args.manifest.read_text())
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(args.output, 0o700)
    prices = (args.input_price, args.cached_price, args.output_price)
    async with AsyncOpenAI(timeout=180, max_retries=0) as client:
        for case_index, case in enumerate(cases, 1):
            images, texts, hashes = [], [], []
            for filename in case['files']:
                path = args.manifest.parent / filename
                raw = path.read_bytes()
                hashes.append(hashlib.sha256(raw).hexdigest())
                if path.suffix.lower() == '.pdf':
                    images.extend(render_pdf_pages(raw))
                    texts.append(extract_pdf_text(raw) or '')
                else:
                    mime = mimetypes.guess_type(path.name)[0] or ''
                    if not mime.startswith('image/'):
                        raise ValueError('Only image and PDF inputs are supported')
                    images.append((raw, mime))
            for repeat in range(1, args.repeats+1):
                variants = [('baseline', baseline), ('candidate', receipt_pipeline)]
                if repeat % 2 == 0:
                    variants.reverse()
                for variant, parser in variants:
                    data = await measure(client, parser, args.model, images, case['categories'],
                                         '\n'.join(texts) or None, prices)
                    data.update(case=case['id'], variant=variant, repeat=repeat, model=args.model,
                                source_hashes=hashes, prices_per_million=prices,
                                baseline_sha256=hashlib.sha256(args.baseline_file.read_bytes()).hexdigest())
                    target = args.output / f'{case_index}-{variant}-{repeat}.json'
                    # Exclusive creation avoids silently overwriting prior measurements.
                    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, 'w') as output:
                        json.dump(data, output, ensure_ascii=False, indent=2)
                    print(json.dumps({k: v for k, v in data.items()
                                      if k not in ('result', 'records')}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--baseline-file', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--input-price', type=float, required=True)
    parser.add_argument('--cached-price', type=float, required=True)
    parser.add_argument('--output-price', type=float, required=True)
    parser.add_argument('--repeats', type=int, default=2)
    asyncio.run(main(parser.parse_args()))
