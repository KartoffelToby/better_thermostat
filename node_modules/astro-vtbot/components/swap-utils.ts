/*
 * These functions are deprecated.
 * Starting from Astro 4.8, please have a look at astro/dist/transitions/swap-functions.js
 */

const PERSIST_ATTR = 'data-astro-transition-persist';

type SavedFocus = {
	activeElement: HTMLElement | null;
	start?: number | null;
	end?: number | null;
};

/*
 * Mark new scripts we already have on the current page as already been executed.
 * This way they will not be executed again
 */
export const disarmKnownScripts = (doc: Document) => {
	[...doc.scripts].forEach((newScript) => {
		newScript.dataset.astroExec = '';
		const found =
			!newScript.hasAttribute('data-astro-rerun') &&
			[...document.scripts].find((oldScript) => oldScript.isEqualNode(newScript));
		if (!found) delete newScript.dataset.astroExec;
	});
};

/*
 * Swap in the atttributes of the html element
 */
export const swapInHTMLAttributes = (doc: Document, rootAttributesToPreserve: string[]) => {
	const preserve = (name: string) =>
		name.startsWith('data-astro-') || rootAttributesToPreserve.includes(name);
	const html = document.documentElement;
	[...html.attributes].forEach(({ name }) => preserve(name) || html.removeAttribute(name));
	[...doc.documentElement.attributes].forEach(
		({ name, value }) => preserve(name) || html.setAttribute(name, value)
	);
};

/*
 * Swap in the children of the head
 */
export const swapInHeadElements = (doc: Document) => {
	[...document.head.children].forEach((e) => {
		if (e instanceof HTMLElement) {
			const id = e.getAttribute(PERSIST_ATTR);
			let other: Element | '' | null | undefined =
				id && doc.head.querySelector(`[${PERSIST_ATTR}="${id}"]`);
			other ||= [...doc.head.children].find((o) => o.isEqualNode(e));
			(other ?? e).remove();
		}
	});
	document.head.append(...doc.head.children);
};

/*
 * Save the active element and the current selection
 */
export const saveFocus = (): SavedFocus => {
	const activeElement = document.activeElement as HTMLElement;
	if (
		(activeElement instanceof HTMLInputElement || activeElement instanceof HTMLTextAreaElement) &&
		activeElement.ownerDocument.location.origin === document.location.origin
	) {
		const start = activeElement.selectionStart;
		const end = activeElement.selectionEnd;
		return { activeElement, start, end };
	}
	return { activeElement };
};

/*
 * Restore the focus and selection if the saved active element still belongs to window.document
 */
export const restoreFocus = ({ activeElement, start, end }: SavedFocus) => {
	if (activeElement) {
		activeElement.focus();
		if (activeElement instanceof HTMLInputElement || activeElement instanceof HTMLTextAreaElement) {
			activeElement.selectionStart = start!;
			activeElement.selectionEnd = end!;
		}
	}
};

export function astroBodySwap(oldEl: Element, newEl: Element) {
	const PERSIST_ATTR = 'data-astro-transition-persist';

	const shouldCopyProps = (el: HTMLElement) => {
		const persistProps = el.dataset.astroTransitionPersistProps;
		return persistProps == null || persistProps === 'false';
	};

	oldEl.replaceWith(newEl);

	for (const el of oldEl.querySelectorAll(`[${PERSIST_ATTR}]`)) {
		const id = el.getAttribute(PERSIST_ATTR);
		const newEl = document.querySelector(`[${PERSIST_ATTR}="${id}"]`);
		if (newEl) {
			newEl.replaceWith(el);
			if (el.localName === 'astro-island' && shouldCopyProps(el as HTMLElement)) {
				el.setAttribute('ssr', '');
				el.setAttribute('props', newEl.getAttribute('props')!);
			}
		}
	}
}

/*
 * Execute all steps of the original swap function except the swap of the body element.
 * Accepts a function to substitute the swap of the body element.
 */
export const customSwap = (
	doc: Document,
	rootAttributesToPreserve: string[] = [],
	swapBody: (doc: Document) => void
) => {
	disarmKnownScripts(doc);
	swapInHTMLAttributes(doc, rootAttributesToPreserve);
	swapInHeadElements(doc);
	const savedFocus = saveFocus();
	swapBody(doc);
	restoreFocus(savedFocus);
};
