import axios from 'axios';
import { CheerioAPI, load } from 'cheerio';

interface CrawlCandidate {
  url: string;
  depth: number;
}

export interface CrawledPage {
  url: string;
  title: string | null;
  statusCode: number | null;
  crawledAt: Date;
}

export interface CrawlCompanyInput {
  websiteUrl: string;
  maxPages: number;
}

const PRIORITY_PATHS = ['/contact', '/about', '/team', '/company'];

function normalizeUrl(url: string): string | null {
  try {
    const parsed = new URL(url);

    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
      return null;
    }

    parsed.hash = '';
    return parsed.toString();
  } catch {
    return null;
  }
}

function buildInternalUrl(base: URL, href: string): string | null {
  try {
    const candidate = new URL(href, base);

    if (candidate.hostname !== base.hostname) {
      return null;
    }

    if (candidate.protocol !== 'http:' && candidate.protocol !== 'https:') {
      return null;
    }

    candidate.hash = '';
    return candidate.toString();
  } catch {
    return null;
  }
}

function extractPriorityLinks($: CheerioAPI, base: URL): string[] {
  const links = new Set<string>();

  $('a[href]').each((_, anchor) => {
    const href = $(anchor).attr('href');

    if (!href) {
      return;
    }

    const normalized = buildInternalUrl(base, href);

    if (!normalized) {
      return;
    }

    const pathname = new URL(normalized).pathname.toLowerCase();
    if (PRIORITY_PATHS.some((path) => pathname === path || pathname.startsWith(`${path}/`))) {
      links.add(normalized);
    }
  });

  return Array.from(links);
}

async function fetchPage(url: string): Promise<{ statusCode: number | null; html: string | null }> {
  try {
    const response = await axios.get<string>(url, {
      timeout: 15000,
      maxRedirects: 5,
      headers: {
        'User-Agent': 'B2BLeadDiscoveryBot/1.0 (+public-crawl)'
      },
      responseType: 'text',
      validateStatus: () => true
    });

    const contentType = String(response.headers['content-type'] ?? '').toLowerCase();
    if (!contentType.includes('text/html')) {
      return { statusCode: response.status, html: null };
    }

    return {
      statusCode: response.status,
      html: typeof response.data === 'string' ? response.data : null
    };
  } catch {
    return { statusCode: null, html: null };
  }
}

export async function crawlCompanyWebsite(input: CrawlCompanyInput): Promise<CrawledPage[]> {
  const startUrl = normalizeUrl(input.websiteUrl);

  if (!startUrl) {
    return [];
  }

  const base = new URL(startUrl);
  const queue: CrawlCandidate[] = [{ url: startUrl, depth: 0 }];
  const visited = new Set<string>();
  const crawled: CrawledPage[] = [];

  while (queue.length > 0 && crawled.length < input.maxPages) {
    const next = queue.shift();

    if (!next || visited.has(next.url)) {
      continue;
    }

    visited.add(next.url);

    const page = await fetchPage(next.url);
    const crawledAt = new Date();
    let title: string | null = null;

    if (page.html) {
      const $ = load(page.html);
      title = $('title').first().text().trim() || null;

      if (next.depth < 1) {
        const priorityLinks = extractPriorityLinks($, base);

        for (const link of priorityLinks) {
          if (!visited.has(link)) {
            queue.push({ url: link, depth: next.depth + 1 });
          }
        }
      }
    }

    crawled.push({
      url: next.url,
      title,
      statusCode: page.statusCode,
      crawledAt
    });
  }

  return crawled;
}
