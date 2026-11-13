import type { APIRoute, GetStaticPaths } from 'astro';
import { getCollection } from 'astro:content';
import { pageMarkdown } from '../llms';

// The Markdown copy of each page, at the page's path with `.md` appended.
export const getStaticPaths = (async () =>
	(await getCollection('docs')).map((doc) => ({
		params: { slug: doc.id },
		props: { doc },
	}))) satisfies GetStaticPaths;

export const GET: APIRoute = ({ props }) =>
	new Response(pageMarkdown(props.doc), {
		headers: { 'Content-Type': 'text/markdown; charset=utf-8' },
	});
