import { WEBUI_API_BASE_URL } from '$lib/constants';
import { splitStream } from '$lib/utils';

export const uploadFile = async (
	token: string,
	file: File,
	metadata?: object | null,
	process?: boolean | null,
	stream: boolean = true
) => {
	const data = new FormData();
	data.append('file', file);
	if (metadata) {
		data.append('metadata', JSON.stringify(metadata));
	}

	const searchParams = new URLSearchParams();
	if (process !== undefined && process !== null) {
		searchParams.append('process', String(process));
	}

	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/files/?${searchParams.toString()}`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			authorization: `Bearer ${token}`
		},
		body: data
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err.detail || err.message;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	if (res && stream) {
		await waitForProcessing(token, res);
	}

	if (error) {
		throw error;
	}

	return res;
};

// Follow a file's processing (text extraction) until it is done, noting errors on `res`.
const waitForProcessing = async (token: string, res: any) => {
	const status = await getFileProcessStatus(token, res.id);

	if (status && status.ok) {
		const reader = status.body
			.pipeThrough(new TextDecoderStream())
			.pipeThrough(splitStream('\n'))
			.getReader();

		while (true) {
			const { value, done } = await reader.read();
			if (done) {
				break;
			}

			try {
				let lines = value.split('\n');

				for (const line of lines) {
					if (line !== '') {
						console.log(line);
						if (line === 'data: [DONE]') {
							console.log(line);
						} else {
							let data = JSON.parse(line.replace(/^data: /, ''));
							console.log(data);

							if (data?.error) {
								console.error(data.error);
								res.error = data.error;
							}

							if (res?.data) {
								res.data = data;
							}
						}
					}
				}
			} catch (error) {
				console.log(error);
			}
		}
	}
};

// Mologate: big files (up to 500 MB) go up in chunks: several at once, each retried on failure,
// gzip-compressed when the file compresses well. See backend/open_webui/routers/uploads.py.
const COMPRESSIBLE =
	/^(text\/|application\/(json|xml|javascript|x-ndjson|x-yaml|yaml|sql|x-sh|csv|x-csv|svg\+xml|x-ipynb\+json|ld\+json|graphql))/;
const COMPRESSIBLE_EXTENSIONS =
	/\.(txt|log|csv|tsv|json|jsonl|ndjson|xml|html?|css|js|mjs|ts|tsx|jsx|py|ipynb|md|yaml|yml|toml|ini|sql|sh|c|h|cpp|java|go|rs|rb|php|svg|tex|srt|vtt)$/i;

const gzip = async (data: Blob): Promise<Blob | null> => {
	if (typeof CompressionStream === 'undefined') return null;
	const stream = data.stream().pipeThrough(new CompressionStream('gzip'));
	return await new Response(stream).blob();
};

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

export const uploadFileChunked = async (
	token: string,
	file: File,
	metadata?: object | null,
	process?: boolean | null,
	onProgress?: (fraction: number) => void,
	signal?: AbortSignal
) => {
	const base = `${WEBUI_API_BASE_URL}/files/uploads`;
	const headers = { authorization: `Bearer ${token}` };
	const json = async (response: Response) => {
		const body = await response.json().catch(() => ({}));
		if (!response.ok) throw body?.detail ?? `HTTP ${response.status}`;
		return body;
	};

	const upload = await json(
		await fetch(base, {
			method: 'POST',
			headers: { ...headers, 'Content-Type': 'application/json' },
			body: JSON.stringify({
				name: file.name,
				size: file.size,
				content_type: file.type || null,
				metadata: metadata ?? null
			}),
			signal
		})
	);

	let compress =
		COMPRESSIBLE.test(file.type ?? '') || COMPRESSIBLE_EXTENSIONS.test(file.name ?? '');
	const done = new Set<number>(upload.received ?? []);
	let sent = done.size * upload.chunk_size;
	onProgress?.(Math.min(1, sent / file.size));

	const sendChunk = async (index: number) => {
		const start = index * upload.chunk_size;
		const chunk = file.slice(start, Math.min(file.size, start + upload.chunk_size));
		let body: Blob = chunk;
		let encoding: string | null = null;
		if (compress) {
			const packed = await gzip(chunk);
			// Not worth it (already compressed data): send this and later chunks as they are.
			if (packed && packed.size < chunk.size * 0.9) {
				body = packed;
				encoding = 'gzip';
			} else {
				compress = false;
			}
		}
		for (let attempt = 0; ; attempt += 1) {
			try {
				const response = await fetch(`${base}/${upload.id}/chunks/${index}`, {
					method: 'PUT',
					headers: {
						...headers,
						'Content-Type': 'application/octet-stream',
						...(encoding ? { 'Content-Encoding': encoding } : {})
					},
					body,
					signal
				});
				if (response.ok) break;
				// A refused chunk (too big, a bomb, not ours) won't get better by sending it again.
				if (response.status < 500 && response.status !== 408 && response.status !== 429) {
					await json(response);
				}
				throw new Error(`HTTP ${response.status}`);
			} catch (error) {
				if (signal?.aborted || typeof error === 'string' || attempt >= 4) throw error;
				await sleep(1000 * 2 ** attempt);
			}
		}
		done.add(index);
		sent += chunk.size;
		onProgress?.(Math.min(1, sent / file.size));
	};

	const queue = Array.from({ length: upload.chunks }, (_, index) => index).filter(
		(index) => !done.has(index)
	);
	const worker = async () => {
		while (queue.length) {
			await sendChunk(queue.shift() as number);
		}
	};
	try {
		await Promise.all(Array.from({ length: Math.min(3, queue.length) }, worker));
	} catch (error) {
		await fetch(`${base}/${upload.id}`, { method: 'DELETE', headers }).catch(() => {});
		throw error;
	}

	const res = await json(
		await fetch(`${base}/${upload.id}/complete`, {
			method: 'POST',
			headers: { ...headers, 'Content-Type': 'application/json' },
			body: JSON.stringify({ process: process ?? true }),
			signal
		})
	);
	if (res?.data?.status === 'pending') {
		await waitForProcessing(token, res);
	}
	return res;
};

export const getFileProcessStatus = async (token: string, id: string) => {
	const queryParams = new URLSearchParams();
	queryParams.append('stream', 'true');

	let error = null;
	const res = await fetch(`${WEBUI_API_BASE_URL}/files/${id}/process/status?${queryParams}`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			authorization: `Bearer ${token}`
		}
	}).catch((err) => {
		error = err.detail;
		console.error(err);
		return null;
	});

	if (error) {
		throw error;
	}

	return res;
};

export const uploadDir = async (token: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/files/upload/dir`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err.detail;
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const getFiles = async (token: string = '', content: boolean = false) => {
	let error = null;

	const searchParams = new URLSearchParams();
	searchParams.append('content', String(content));

	const res = await fetch(`${WEBUI_API_BASE_URL}/files/?${searchParams.toString()}`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err.detail;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const searchFiles = async (
	token: string,
	filename: string = '*',
	skip: number = 0,
	limit: number = 50,
	content: boolean = false
) => {
	let error = null;

	const searchParams = new URLSearchParams();
	searchParams.append('filename', filename);
	searchParams.append('skip', String(skip));
	searchParams.append('limit', String(limit));
	searchParams.append('content', String(content));

	const res = await fetch(`${WEBUI_API_BASE_URL}/files/search?${searchParams.toString()}`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err.detail;
			console.error(err);
			return [];
		});

	if (error) {
		throw error;
	}

	return res;
};

export const getFileCount = async (token: string = '') => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/files/count`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const getFileById = async (token: string, id: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/files/${id}`, {
		method: 'GET',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err.detail;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const updateFileDataContentById = async (token: string, id: string, content: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/files/${id}/data/content/update`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify({
			content: content
		})
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err.detail;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const getFileContentById = async (id: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/files/${id}/content`, {
		method: 'GET',
		headers: {
			Accept: 'application/json'
		},
		credentials: 'include'
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return await res.arrayBuffer();
		})
		.catch((err) => {
			error = err.detail;
			console.error(err);

			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const renameFileById = async (token: string, id: string, filename: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/files/${id}/rename`, {
		method: 'POST',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		},
		body: JSON.stringify({ filename })
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.catch((err) => {
			error = err.detail;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const deleteFileById = async (token: string, id: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/files/${id}`, {
		method: 'DELETE',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err.detail;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};

export const deleteAllFiles = async (token: string) => {
	let error = null;

	const res = await fetch(`${WEBUI_API_BASE_URL}/files/all`, {
		method: 'DELETE',
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			authorization: `Bearer ${token}`
		}
	})
		.then(async (res) => {
			if (!res.ok) throw await res.json();
			return res.json();
		})
		.then((json) => {
			return json;
		})
		.catch((err) => {
			error = err.detail;
			console.error(err);
			return null;
		});

	if (error) {
		throw error;
	}

	return res;
};
