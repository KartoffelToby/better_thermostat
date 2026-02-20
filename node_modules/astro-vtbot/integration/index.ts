import type { AstroIntegration } from 'astro';
import vitePluginVtbotExtend from './vite-plugin-extend';

import icon from '../assets/bag-of-tricks-mono.svg?raw';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

type VtBotOptions = {
	autoLint?: boolean;
	loadingIndicator?: boolean;
};

export default function createIntegration(options?: VtBotOptions): AstroIntegration {
	return {
		name: 'astro-vtbot',
		hooks: {
			'astro:config:setup': (setupOptions) => {
				const linter = options?.autoLint ?? false;
				const loading = options?.loadingIndicator ?? false;
				if (linter || loading) {
					setupOptions.updateConfig({
						vite: {
							plugins: [vitePluginVtbotExtend({ linter, loading })],
						},
					});
				}

				if (import.meta.env.DEV) {
					setupOptions.injectRoute({
						pattern: '/_vtbot_inspection_chamber.js',
						entrypoint: path.resolve(
							fileURLToPath(import.meta.url),
							'../astro-inspection-chamber.js.ts'
						),
					});

					setupOptions.injectScript(
						'head-inline',
						`(function(){function S(){const s=document.createElement('script');s.src='/_vtbot_inspection_chamber.js';s.blocking="render";document.head.appendChild(s)}; S(); document.addEventListener('astro:after-swap', S)})();`
					);
				}

				setupOptions.addDevToolbarApp({
					id: 'vtbot',
					name: 'The Bag',
					icon,
					entrypoint: fileURLToPath(new URL('../devToolbar/app.ts', import.meta.url)),
				});
			},
		},
	};
}
