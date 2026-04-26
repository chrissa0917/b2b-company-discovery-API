import axios from 'axios';

export interface CompanyDiscoveryInput {
  keyword: string;
  location: string;
  limit: number;
}

export interface DiscoveredCompany {
  name: string;
  websiteUrl: string | null;
  sourceUrl: string;
}

interface CompanyDiscoveryProvider {
  discover(input: CompanyDiscoveryInput): Promise<DiscoveredCompany[]>;
}

class TavilyCompanyDiscoveryProvider implements CompanyDiscoveryProvider {
  constructor(private readonly apiKey: string) {}

  async discover(input: CompanyDiscoveryInput): Promise<DiscoveredCompany[]> {
    const query = `${input.keyword} in ${input.location}`;

    const response = await axios.post(
      'https://api.tavily.com/search',
      {
        api_key: this.apiKey,
        query,
        max_results: input.limit,
        search_depth: 'basic'
      },
      {
        timeout: 15000
      }
    );

    const results = Array.isArray(response.data?.results) ? response.data.results : [];

    return results.slice(0, input.limit).map((result: { title?: string; url?: string }) => ({
      name: result.title?.trim() || 'Unknown Company',
      websiteUrl: result.url ?? null,
      sourceUrl: result.url ?? 'https://api.tavily.com/search'
    }));
  }
}

class SerpApiCompanyDiscoveryProvider implements CompanyDiscoveryProvider {
  constructor(private readonly apiKey: string) {}

  async discover(input: CompanyDiscoveryInput): Promise<DiscoveredCompany[]> {
    const query = `${input.keyword} ${input.location}`;

    const response = await axios.get('https://serpapi.com/search.json', {
      params: {
        q: query,
        num: input.limit,
        engine: 'google',
        api_key: this.apiKey
      },
      timeout: 15000
    });

    const results = Array.isArray(response.data?.organic_results) ? response.data.organic_results : [];

    return results.slice(0, input.limit).map((result: { title?: string; link?: string }) => ({
      name: result.title?.trim() || 'Unknown Company',
      websiteUrl: result.link ?? null,
      sourceUrl: result.link ?? 'https://serpapi.com/search.json'
    }));
  }
}

class MockCompanyDiscoveryProvider implements CompanyDiscoveryProvider {
  async discover(input: CompanyDiscoveryInput): Promise<DiscoveredCompany[]> {
    return Array.from({ length: input.limit }).map((_, index) => {
      const n = index + 1;
      const normalizedKeyword = input.keyword.toLowerCase().replace(/\s+/g, '-');
      const normalizedLocation = input.location.toLowerCase().replace(/\s+/g, '-');

      return {
        name: `${input.keyword} Company ${n}`,
        websiteUrl: `https://${normalizedKeyword}-${normalizedLocation}-${n}.example.com`,
        sourceUrl: 'mock://company-discovery'
      };
    });
  }
}

export function buildCompanyDiscoveryService(provider: string, tavilyApiKey?: string, serpApiKey?: string): CompanyDiscoveryProvider {
  if (provider === 'tavily') {
    if (!tavilyApiKey) {
      throw new Error('DISCOVERY_PROVIDER is set to tavily but TAVILY_API_KEY is missing');
    }

    return new TavilyCompanyDiscoveryProvider(tavilyApiKey);
  }

  if (provider === 'serpapi') {
    if (!serpApiKey) {
      throw new Error('DISCOVERY_PROVIDER is set to serpapi but SERPAPI_API_KEY is missing');
    }

    return new SerpApiCompanyDiscoveryProvider(serpApiKey);
  }

  return new MockCompanyDiscoveryProvider();
}
