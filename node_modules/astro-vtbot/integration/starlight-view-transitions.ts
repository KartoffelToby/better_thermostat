import type { HookParameters, StarlightPlugin } from '@astrojs/starlight/types';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import vtbot from 'astro-vtbot';

interface ViewTransitionOptions {
	declarativeNames?: string | undefined;
	allPages?: string | undefined;
	directionTypes?: string | undefined;
	directionAttribute?: string | undefined;
	camShaftNames?: string | undefined;
}
export function viewTransitions(options: ViewTransitionOptions = {}): StarlightPlugin {
	return {
		name: 'starlight-view-transitions',
		hooks: {
			'config:setup'(sl: HookParameters<'config:setup'>) {
				set('vtbagDeclarativeNames', options.declarativeNames);
				set('vtbagAllPages', options.allPages ?? 'header a, nav.sidebar li a');
				set('vtbagDirectionTypes', options.directionTypes);
				set('vtbagDirectionAttribute', options.directionAttribute);
				set('vtbagCamShaftNames', options.camShaftNames);
				sl.astroConfig.integrations.find(({ name }) => name === 'astro-vtbot') ||
					sl.addIntegration(vtbot());
				sl.addRouteMiddleware({ entrypoint: 'astro-vtbot/integration/starlight-middleware' });
				const css = resolve(
					fileURLToPath(import.meta.url),
					'../../styles/starlight-view-transitions.css'
				);
				sl.updateConfig({
					customCss: [...new Set([css, ...(sl.config.customCss ?? [])])],
				});
			},
		},
	};
}

export function remarkEndOfMarkdown(id = 'end-of-markdown') {
	return function (tree: any) {
		process.env.vtbotEndOfContentId = id;
		tree.children.push({
			type: 'html',
			value: `<a id="${id}"></a>`,
		});
	};
}

function set(property: string, value: any) {
	if (value === undefined) delete process.env[property];
	else process.env[property] = value;
}
