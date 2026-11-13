import type { APIRoute } from 'astro';
import { facts, markdownUrl, sections, summary } from '../llms';

// The index of the documentation for language models: what the project is and
// one link per page, each to the page's Markdown copy.
export const GET: APIRoute = async () => {
	const lists = (await sections()).map(
		({ label, pages }) =>
			`## ${label}\n\n` +
			pages
				.map((doc) => `- [${doc.data.title}](${markdownUrl(doc)}): ${doc.data.description}`)
				.join('\n'),
	);
	const text = [
		'# Better Thermostat',
		`> ${summary}`,
		facts.map((fact) => `- ${fact}`).join('\n'),
		...lists,
	].join('\n\n');
	return new Response(`${text}\n`, { headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
};
