export interface ContactScoreInput {
  email: string;
  sourceUrl: string;
  companyWebsiteUrl: string | null;
}

export function scoreContact(input: ContactScoreInput): number {
  const email = input.email.toLowerCase();
  const sourceUrl = input.sourceUrl.toLowerCase();
  const localPart = email.split('@')[0] ?? '';

  let score = 40;

  if (sourceUrl.includes('/contact') || sourceUrl.includes('/team') || sourceUrl.includes('/about')) {
    score += 20;
  }

  if (/(sales|bizdev|partnership|hello|contact|marketing)/.test(localPart)) {
    score += 15;
  }

  if (/(info|support|admin)/.test(localPart)) {
    score -= 10;
  }

  if (input.companyWebsiteUrl) {
    try {
      const websiteHost = new URL(input.companyWebsiteUrl).hostname.replace(/^www\./, '');
      const emailDomain = (email.split('@')[1] ?? '').replace(/^www\./, '');

      if (websiteHost === emailDomain || websiteHost.endsWith(`.${emailDomain}`) || emailDomain.endsWith(`.${websiteHost}`)) {
        score += 15;
      }
    } catch {
      score -= 5;
    }
  }

  return Math.max(0, Math.min(100, score));
}
