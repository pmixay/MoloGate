// Mologate: the change records saved on finished responses (backend/open_webui/utils/change_sources.py).

export type FileChange = {
	path: string;
	status: 'added' | 'modified' | 'deleted' | string;
	added: number | null;
	removed: number | null;
	diff?: string;
	diff_truncated?: boolean;
};

export type ChangeSet = {
	files: FileChange[];
	total: number;
	added: number;
	removed: number;
	truncated?: boolean;
	base?: string;
	branch?: string;
};

export type ChangeRecord = ChangeSet & {
	source?: 'session' | 'git' | string;
	// What isn't committed yet against the linked branch's base; only a repository source fills it in.
	workspace?: ChangeSet;
};

export type RepositoryLink = {
	provider: 'github';
	name: string;
	branch?: string;
	base?: string;
	path?: string;
};

// A chat's linked repository (`meta.repository`). Chats are linked when they're created; in a chat
// without one, repository features such as the changes bar above the message box stay off.
export const chatRepository = (meta: any): RepositoryLink | null => {
	const link = meta?.repository;
	if (!link || link.provider !== 'github' || typeof link.name !== 'string') return null;
	return /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(link.name) ? link : null;
};

// The changes in a linked chat so far: the latest response's `workspace` when a repository source
// reported one, else every response's files on the current branch added up.
export const chatChanges = (history: any): ChangeSet | null => {
	const records: ChangeRecord[] = [];
	let id = history?.currentId;
	for (let steps = 0; id && steps < 10000; steps += 1) {
		const message = history.messages?.[id];
		if (!message) break;
		if (message.changes?.workspace) return message.changes.workspace;
		if (message.changes?.total) records.push(message.changes);
		id = message.parentId;
	}
	const files = new Map<string, FileChange>();
	for (const record of records.reverse()) {
		for (const file of record.files ?? []) {
			const seen = files.get(file.path);
			// A file the chat created and then deleted leaves nothing behind.
			if (seen?.status === 'added' && file.status === 'deleted') {
				files.delete(file.path);
				continue;
			}
			files.set(file.path, {
				path: file.path,
				// A file the chat created stays "added" however often it is edited afterwards.
				status: seen?.status === 'added' ? 'added' : file.status,
				added:
					seen?.added === null || file.added === null
						? null
						: (seen?.added ?? 0) + (file.added ?? 0),
				removed:
					seen?.removed === null || file.removed === null
						? null
						: (seen?.removed ?? 0) + (file.removed ?? 0)
			});
		}
	}
	const list = [...files.values()].sort((a, b) => a.path.localeCompare(b.path));
	return list.length
		? {
				files: list,
				total: list.length,
				added: list.reduce((sum, file) => sum + (file.added ?? 0), 0),
				removed: list.reduce((sum, file) => sum + (file.removed ?? 0), 0)
			}
		: null;
};

export const splitPath = (path: string) => {
	const index = path.lastIndexOf('/');
	return index < 0 ? ['', path] : [path.slice(0, index + 1), path.slice(index + 1)];
};

// The hunks of a unified diff, without the ---/+++ header the file row already names.
export const diffHunks = (diff: string) => {
	const start = diff.indexOf('\n@@');
	return diff.startsWith('@@') ? diff : start < 0 ? diff : diff.slice(start + 1);
};
