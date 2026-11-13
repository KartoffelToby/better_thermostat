import type { APIRoute } from 'astro';
import { pageMarkdown, sections } from '../llms';

// Every page of the documentation in one Markdown file, in sidebar order.
export const GET: APIRoute = async () => {
	const pages = (await sections()).flatMap((section) => section.pages);
	return new Response(pages.map(pageMarkdown).join('\n---\n\n'), {
		headers: { 'Content-Type': 'text/plain; charset=utf-8' },
	});
};
