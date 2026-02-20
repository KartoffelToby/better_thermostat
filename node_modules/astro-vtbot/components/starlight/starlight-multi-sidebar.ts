import * as STARLIGHT from './utils';

export function updateMultiSidebar(doc: Document) {
	doc
		.querySelectorAll(STARLIGHT.SIDEBAR_CONTENT + ' .__collapse input')
		.forEach((el, idx) => el.setAttribute('data-vtbot-replace', `vtbot-sms-v0-${idx}`));
	doc
		.querySelectorAll(
			STARLIGHT.SIDEBAR_CONTENT +
				' :is(starlight-multi-sidebar-tabs, starlight-multi-sidebar-select)'
		)
		.forEach((sms, index) => {
			sms.setAttribute('data-vtbot-replace', `vtbot-sms-${index}`);
			[...sms.children].forEach((el, idx) => {
				if (idx > 0) {
					[...el.children].forEach((e, i) =>
						e.setAttribute('data-astro-transition-persist', `vtbot-sms-${index}-${idx}-${i}`)
					);
				}
			});
		});
}
