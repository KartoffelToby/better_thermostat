import { spawnSync } from 'node:child_process';
import { existsSync, realpathSync } from 'node:fs';
import { basename, dirname } from 'node:path';
import { defineRouteMiddleware } from '@astrojs/starlight/route-data';

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

export const onRequest = defineRouteMiddleware((context) => {
	const route = context.locals.starlightRoute;
	if (route.editUrl) {
		route.editUrl = new URL(route.editUrl.href.replace('/src/content/docs/', '/docs/'));
	}
	if (route.entry.data.lastUpdated !== false && !route.lastUpdated) {
		route.lastUpdated = newestCommitDate(route.entry.filePath);
	}
});
