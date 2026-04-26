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

export interface ComplianceInput {
  email: string;
  sourceUrl: string;
  suppressedEmails: Set<string>;
}

export function isCompliantBusinessContact(input: ComplianceInput): boolean {
  const email = input.email.trim().toLowerCase();
  const sourceUrl = input.sourceUrl.trim().toLowerCase();
  const [, domain] = email.split('@');

  if (!domain) {
    return false;
  }

  if (PERSONAL_EMAIL_DOMAINS.has(domain)) {
    return false;
  }

  if (input.suppressedEmails.has(email)) {
    return false;
  }

  if (!sourceUrl.startsWith('http://') && !sourceUrl.startsWith('https://')) {
    return false;
  }

  if (sourceUrl.includes('linkedin.com')) {
    return false;
  }

  return true;
}
