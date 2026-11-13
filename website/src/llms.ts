import { getCollection, type CollectionEntry } from 'astro:content';
import hacs from '../../hacs.json';
import { sidebar } from './sidebar.mjs';

// Shared by /llms.txt, /llms-full.txt and the per-page Markdown files: the
// plain-text copy of the documentation that language models and agents read
// instead of the rendered HTML (https://llmstxt.org/).

type Doc = CollectionEntry<'docs'>;

const site = import.meta.env.SITE;
const repository = 'https://github.com/KartoffelToby/better_thermostat';
// The directory of the sidebar group llms.txt lists as "Optional": pages a
// model can skip when it only has room for the user documentation.
const optionalDirectory = 'internals';

export const summary =
	'Better Thermostat is a free, open-source custom integration for Home Assistant. It wraps one or more smart radiator valves (TRVs) and a separate room temperature sensor into one virtual `climate` entity and keeps correcting the valves so that the room, not the radiator, reaches the target temperature.';

export const facts = [
	`Installed through HACS (the Home Assistant Community Store) and set up per room in the Home Assistant UI; the integration domain is \`better_thermostat\`. Source code, issues and discussions: ${repository}`,
	`Requires Home Assistant ${hacs.homeassistant} or newer and, per room, at least one TRV or wall thermostat (\`climate.*\`) plus one separate room temperature sensor (\`sensor.*\`).`,
	'Controls how a room reaches and holds its temperature, not when: it has no built-in timetable. Schedules come from the bundled automation blueprints or from Home Assistant automations.',
	'Once a room is set up, dashboards, schedules, automations and voice assistants control only the Better Thermostat entity, never the physical TRVs directly.',
	`Every page is also served as Markdown: append \`.md\` to its path, as the links below do. The whole documentation in one file: ${site}/llms-full.txt`,
];

export function pageUrl(doc: Doc): string {
	return doc.id === 'index' ? `${site}/` : `${site}/${doc.id}/`;
}

export function markdownUrl(doc: Doc): string {
	return `${site}/${doc.id}.md`;
}

/** The pages in sidebar order, grouped like the sidebar, with the developer internals last. */
export async function sections(): Promise<{ label: string; pages: Doc[] }[]> {
	const docs = new Map((await getCollection('docs')).map((doc) => [doc.id, doc]));
	const take = (id: string): Doc[] => {
		const doc = docs.get(id);
		docs.delete(id);
		return doc ? [doc] : [];
	};
	const takeDirectory = (directory: string): Doc[] =>
		[...docs.values()]
			.filter((doc) => doc.id.startsWith(`${directory}/`))
			.sort((a, b) => (a.data.sidebar.order ?? 0) - (b.data.sidebar.order ?? 0))
			.flatMap((doc) => take(doc.id));

	const home = take('index');
	const optional = takeDirectory(optionalDirectory);
	const listed = sidebar
		.map((group) => ({
			label: group.label,
			pages: (group.items ?? [group]).flatMap((item) =>
				'link' in item && item.link ? take(item.link.replace(/^\/|\/$/g, '')) : [],
			),
		}))
		.filter((section) => section.pages.length > 0);

	return [
		{ label: 'Overview', pages: home },
		...listed,
		// A page no sidebar group links to is still part of the documentation.
		...(docs.size > 0 ? [{ label: 'More', pages: [...docs.values()] }] : []),
		{ label: 'Optional', pages: optional },
	];
}

/** One page as Markdown: MDX components dropped, links made absolute. */
export function pageMarkdown(doc: Doc): string {
	let body = doc.body ?? '';
	if (doc.filePath?.endsWith('.mdx')) {
		body = body
			.replace(/^import .+ from .+;\n/gm, '')
			.replace(
				/^[ \t]*<Card title="([^"]+)"[^>]*>([\s\S]*?)<\/Card>/gm,
				(_, title: string, text: string) => `- **${title}:** ${text.trim().replace(/\s*\n\s*/g, ' ')}`,
			)
			.replace(/<\/?[A-Z][^>]*>\n?/g, '');
	}
	// Site-absolute links point at the site, relative ones at the file in the
	// repository, where the images the pages embed are stored.
	const source = `${repository}/raw/master/${doc.filePath?.replace('src/content/docs/', 'docs/')}`;
	body = body.replace(/\]\((\.{0,2}\/[^)\s]*)\)/g, (_, target: string) =>
		target.startsWith('/') ? `](${site}${target})` : `](${new URL(target, source)})`,
	);
	return `# ${doc.data.title}\n\n> ${doc.data.description}\n\nSource: ${pageUrl(doc)}\n\n${body.trim()}\n`;
}
