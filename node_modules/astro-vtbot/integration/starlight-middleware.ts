/// <reference path="./env.d.ts" />
import { defineRouteMiddleware } from '@astrojs/starlight/route-data';
import signal from '@vtbag/turn-signal?url&no-inline';
import shaft from '@vtbag/cam-shaft?url&no-inline';
import names from '@vtbag/utensil-drawer/declarative-names?url&no-inline';

export const onRequest = defineRouteMiddleware((context) => {
	const allPages = process.env.vtbagAllPages;
	const declarativeNames = process.env.vtbagDeclarativeNames;
	const directionTypes = process.env.vtbagDirectionTypes;
	const directionAttribute = process.env.vtbagDirectionAttribute;
	const camShaftNames = process.env.vtbagCamShaftNames;
	const data = context.locals.starlightRoute.entry.data;
	const head = context.locals.starlightRoute.head ?? data.head;
	if (!expect(data.renderBlocking, head)) {
		// might be set by remarkEndOfMarkdownId
		expect(process.env.vtbotEndOfContentId, head);
	}
	{
		const attrs = { src: shaft, async: true, blocking: 'render' };
		camShaftNames ?? (attrs['data-view-transition-names'] = camShaftNames);
		head.push({ tag: 'script', attrs, content: '' });
	}
	{
		const attrs = { src: signal, async: true, blocking: 'render' };
		allPages === undefined || (attrs['data-selector'] = allPages);
		directionTypes === undefined || (attrs['data-direction-types'] = directionTypes);
		directionAttribute === undefined || (attrs['data-direction-attribute'] = directionAttribute);
		head.push({ tag: 'script', attrs, content: '' });
	}
	{
		const attrs = {
			src: names,
			async: true,
			blocking: 'render',
			'data-vtbag-decl': declarativeNames,
		};
		declarativeNames && head.push({ tag: 'script', attrs, content: '' });
	}
});

function expect(id: string | undefined, head: {}[]) {
	if (id) {
		const href = (id.startsWith('#') ? '' : '#') + id;
		head.push({ tag: 'link', attrs: { rel: 'expect', href, blocking: 'render' } });
		return true;
	}
	return false;
}
