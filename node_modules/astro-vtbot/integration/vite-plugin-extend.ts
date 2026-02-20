import { parse } from 'acorn';
import { walk, type Node } from 'estree-walker';
import type { Plugin } from 'vite';

type ExtendOptions = {
	linter: boolean;
	loading: boolean;
};

export default function vitePluginVtbotExtend(opts: ExtendOptions): Plugin {
	return {
		name: 'vtbot:linter',
		enforce: 'pre',
		transform(code: string, id: string) {
			let { linter, loading } = opts;
			if (!import.meta.env.DEV) linter = false;
			if ((!linter && !loading) || id.match(/vtpl[123]\.astro$/) || !id.endsWith('.astro')) return;

			const replacement = `"astro-vtbot/vtex${loading ? (linter ? '3' : '2') : '1'}"`;
			const match = code.match(/from\s*['"]astro:transitions["']/ms);
			if (match) {
				const ast = parse(code, {
					ecmaVersion: 'latest',
					sourceType: 'module',
				}) as Node;

				walk(ast, {
					enter(node: any) {
						if (node.type === 'ImportDeclaration' && node.source.value === 'astro:transitions') {
							code =
								code.substring(0, node.source.start) +
								replacement +
								code.substring(node.source.end);
						}
					},
				});
			}
			return code;
		},
	};
}
