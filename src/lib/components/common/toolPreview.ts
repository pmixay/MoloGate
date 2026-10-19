/** Compact plain-text arguments for a tool row, including collapsed and completed calls. */
export const getToolPreview = (args: Record<string, unknown> | null): string => {
	if (!args) return '';
	for (const key of [
		'command',
		'cmd',
		'query',
		'url',
		'path',
		'file_path',
		'pattern',
		'searchQuery',
		'task'
	]) {
		const value = args[key];
		if (typeof value !== 'string') continue;
		const line = value
			.split('\n')
			.map((part) => part.trim())
			.find(Boolean);
		if (!line) continue;
		// Strip terminal control characters and bound the visible preview. Full args remain expandable.
		const text = line.replace(/[\u0000-\u001f\u007f]/g, ' ').replace(/\s+/g, ' ');
		return text.length > 100 ? `${text.slice(0, 99)}…` : text;
	}
	return '';
};
