import { spawnSync } from 'node:child_process';
import { existsSync, realpathSync } from 'node:fs';
import { basename, dirname } from 'node:path';
import { defineRouteMiddleware, type StarlightRouteData } from '@astrojs/starlight/route-data';
import manifest from '../../custom_components/better_thermostat/manifest.json';

const repository = 'https://github.com/KartoffelToby/better_thermostat';

// src/content/docs is a symlink to the repository's docs/ directory. Starlight
// derives the edit link and the last-updated date from the link's path, which
// GitHub cannot edit through and git has no history for.

function newestCommitDate(file: string): Date | undefined {
	// Starlight's fallback 404 page has an entry but no file.
	if (!existsSync(file)) return undefined;
	const realFile = realpathSync(file);
	const result = spawnSync('git', ['log', '--max-count=1', '--format=%ct', '--', basename(realFile)], {
		cwd: dirname(realFile),
		encoding: 'utf-8',
	});
	const timestamp = Number.parseInt(result.stdout?.trim() ?? '', 10);
	return Number.isNaN(timestamp) ? undefined : new Date(timestamp * 1000);
}

// What a page is, in schema.org terms, for search engines and language models:
// the home page describes the site and the integration, every other page is an
// article about it with its place below the home page.
function structuredData(route: StarlightRouteData, url: string, home: string): object[] {
	const { title, description } = route.entry.data;
	const image = `${home}og.png`;
	const website = { '@type': 'WebSite', '@id': `${home}#website`, url: home, name: route.siteTitle };
	if (url === home) {
		return [
			{ ...website, description, inLanguage: route.lang },
			{
				'@type': 'SoftwareApplication',
				'@id': `${home}#software`,
				name: manifest.name,
				description,
				url: home,
				image,
				applicationCategory: 'UtilitiesApplication',
				applicationSubCategory: 'Home automation',
				operatingSystem: 'Home Assistant',
				softwareVersion: manifest.version,
				license: 'https://www.gnu.org/licenses/agpl-3.0.html',
				isAccessibleForFree: true,
				offers: { '@type': 'Offer', price: '0', priceCurrency: 'EUR' },
				downloadUrl: `${repository}/releases`,
				sameAs: [repository],
			},
		];
	}
	return [
		{
			'@type': 'TechArticle',
			headline: title,
			description,
			url,
			mainEntityOfPage: url,
			image,
			inLanguage: route.lang,
			dateModified: route.lastUpdated?.toISOString(),
			isPartOf: website,
			about: { '@id': `${home}#software`, '@type': 'SoftwareApplication', name: manifest.name, url: home },
			author: {
				'@type': 'Organization',
				name: `${manifest.name} contributors`,
				url: `${repository}/graphs/contributors`,
			},
			publisher: {
				'@type': 'Organization',
				name: manifest.name,
				url: home,
				logo: { '@type': 'ImageObject', url: `${home}apple-touch-icon.png` },
			},
		},
		{
			'@type': 'BreadcrumbList',
			itemListElement: [
				{ '@type': 'ListItem', position: 1, name: route.siteTitle, item: home },
				{ '@type': 'ListItem', position: 2, name: title, item: url },
			],
		},
	];
}

export const onRequest = defineRouteMiddleware((context) => {
	const route = context.locals.starlightRoute;
	if (route.editUrl) {
		route.editUrl = new URL(route.editUrl.href.replace('/src/content/docs/', '/docs/'));
	}
	if (route.entry.data.lastUpdated !== false && !route.lastUpdated) {
		route.lastUpdated = newestCommitDate(route.entry.filePath);
	}

	// The canonical link is absent in a build without a site URL; the fallback
	// 404 page is no page of the documentation.
	const canonical = route.head.find(({ tag, attrs }) => tag === 'link' && attrs?.rel === 'canonical');
	const url = canonical?.attrs?.href;
	if (typeof url !== 'string' || route.entry.id === '404') return;

	if (route.lastUpdated) {
		// The sitemap takes each page's <lastmod> from this tag.
		route.head.push({
			tag: 'meta',
			attrs: { property: 'article:modified_time', content: route.lastUpdated.toISOString() },
		});
	}
	route.head.push(
		// src/pages/[...slug].md.ts serves the page's Markdown copy; the home
		// page has an empty id here and `index` in the collection.
		{
			tag: 'link',
			attrs: { rel: 'alternate', type: 'text/markdown', href: `/${route.entry.id || 'index'}.md` },
		},
		{
			tag: 'script',
			attrs: { type: 'application/ld+json' },
			content: JSON.stringify({
				'@context': 'https://schema.org',
				'@graph': structuredData(route, url, new URL('/', url).href),
			}).replaceAll('<', '\\u003c'),
		},
	);
});
