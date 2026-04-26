import axios from 'axios';
import { load } from 'cheerio';

const EMAIL_REGEX = /([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})/g;

const PERSONAL_EMAIL_DOMAINS = new Set([
  'gmail.com',
  'yahoo.com',
  'outlook.com',
  'hotmail.com',
  'icloud.com',
  'aol.com',
  'live.com',
  'msn.com',
  'proton.me',
  'protonmail.com'
]);

export interface ExtractedEmail {
  email: string;
  sourceUrl: string;
}

export function normalizeEmail(raw: string): string {
  return raw.trim().toLowerCase().replace(/^mailto:/, '').replace(/[),.;]+$/g, '');
}

export function isBusinessEmail(email: string): boolean {
  const normalized = normalizeEmail(email);
  const [local, domain] = normalized.split('@');

  if (!local || !domain) {
    return false;
  }

  if (PERSONAL_EMAIL_DOMAINS.has(domain)) {
    return false;
  }

  if (local.startsWith('noreply') || local.startsWith('no-reply')) {
    return false;
  }

  return true;
}

export async function extractEmailsFromUrl(url: string): Promise<ExtractedEmail[]> {
  try {
    const response = await axios.get<string>(url, {
      timeout: 15000,
      maxRedirects: 5,
      responseType: 'text',
      headers: {
        'User-Agent': 'B2BLeadDiscoveryBot/1.0 (+email-extractor)'
      },
      validateStatus: () => true
    });

    const contentType = String(response.headers['content-type'] ?? '').toLowerCase();
    if (!contentType.includes('text/html')) {
      return [];
    }

    const html = typeof response.data === 'string' ? response.data : '';
    const $ = load(html);

    const found = new Set<string>();

    const mailtoLinks = $('a[href^="mailto:"]')
      .map((_, el) => String($(el).attr('href') ?? ''))
      .get();

    for (const value of mailtoLinks) {
      const email = normalizeEmail(value);
      if (isBusinessEmail(email)) {
        found.add(email);
      }
    }

    const text = $('body').text();
    const matches = text.match(EMAIL_REGEX) ?? [];

    for (const rawMatch of matches) {
      const email = normalizeEmail(rawMatch);
      if (isBusinessEmail(email)) {
        found.add(email);
      }
    }

    return Array.from(found).map((email) => ({
      email,
      sourceUrl: url
    }));
  } catch {
    return [];
  }
}
