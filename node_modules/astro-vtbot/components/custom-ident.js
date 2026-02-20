export function encode(name) {
	const res = [];
	const match = name.match(/^-*[0-9]/);
	if (match) {
		res.push(match[0].slice(0, -1));
		res.push('\\3' + match[0].slice(-1));
		name = name.slice(match[0].length);
	}
	for (const char of name) {
		if (char.match(/[-A-Za-z0-9_]/)) {
			res.push(char);
		} else {
			const point = char.codePointAt(0);
			if (!point) continue;
			if (point < 128) {
				res.push('_' + point.toString(16).toUpperCase());
			} else {
				res.push('\\' + point.toString(16).toUpperCase());
			}
		}
	}
	const result = res
		.map((s, idx, arr) =>
			idx > 0 && arr[idx - 1][0] === '\\' && arr[idx - 1].length !== 7 && s.match(/^[0-9A-Fa-f]/)
				? ' ' + s
				: s
		)
		.join('');
	return result;
}
