<script lang="ts">
	import hljs from 'highlight.js';
	import { toast } from 'svelte-sonner';
	import { getContext, onMount, tick, onDestroy } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';
	import { config, pyodideWorker as pyodideWorkerStore } from '$lib/stores';

	import { createPyodideWorker } from '$lib/pyodide/createPyodideWorker';
	import { executeCode } from '$lib/apis/utils';
	import {
		copyToClipboard,
		initMermaid,
		renderMermaidDiagram,
		renderVegaVisualization,
		unescapeHtml
	} from '$lib/utils';

	import 'highlight.js/styles/github-dark.min.css';
	import equal from 'fast-deep-equal';

	import CodeEditor from '$lib/components/common/CodeEditor.svelte';
	import DiffBlock from './DiffBlock.svelte';
	import { nocturneCodeMirror } from './codeTheme';
	import SvgPanZoom from '$lib/components/common/SVGPanZoom.svelte';

	import ChevronUp from '$lib/components/icons/ChevronUp.svelte';
	import ChevronUpDown from '$lib/components/icons/ChevronUpDown.svelte';
	import CommandLine from '$lib/components/icons/CommandLine.svelte';
	import Cube from '$lib/components/icons/Cube.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';

	const i18n = getContext<Writable<i18nType>>('i18n');

	export let id = '';
	export let edit = true;

	export let onSave = (e) => {};
	export let onUpdate = (e, codeBlockId = '') => {};
	export let onPreview = (e) => {};

	export let save = false;
	export let run = true;
	export let preview = false;
	export let collapsed = false;

	export let token;
	export let lang = '';
	export let code = '';
	export let attributes = {};

	export let className = '';
	export let editorClassName = '';
	export let stickyButtonsClassName = 'top-0';

	let localPyodideWorker = null;

	let _code = '';
	$: _code = code;
	$: isDiff = ['diff', 'patch'].includes(lang.trim().toLowerCase());
	let editingDiff = false;

	let _token = null;

	let renderHTML = null;
	let renderError = null;

	let highlightedCode = null;
	let executing = false;

	let stdout = null;
	let stderr = null;
	let result = null;
	let files = null;

	let copied = false;
	let saved = false;

	const headerButtonClassName =
		'flex items-center gap-1.5 h-7 px-2 rounded-[6px] text-[12px] leading-none text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-850 transition focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent';

	const collapseCodeBlock = () => {
		collapsed = !collapsed;
	};

	const saveCode = () => {
		saved = true;

		code = _code;
		onSave(code);

		setTimeout(() => {
			saved = false;
		}, 1000);
	};

	const copyCode = async () => {
		copied = true;
		await copyToClipboard(_code);

		setTimeout(() => {
			copied = false;
		}, 1000);
	};

	const previewCode = () => {
		onPreview(code);
	};

	const checkPythonCode = (str) => {
		// Check if the string contains typical Python syntax characters
		const pythonSyntax = [
			'def ',
			'else:',
			'elif ',
			'try:',
			'except:',
			'finally:',
			'yield ',
			'lambda ',
			'assert ',
			'nonlocal ',
			'del ',
			'True',
			'False',
			'None',
			' and ',
			' or ',
			' not ',
			' in ',
			' is ',
			' with '
		];

		for (let syntax of pythonSyntax) {
			if (str.includes(syntax)) {
				return true;
			}
		}

		// If none of the above conditions met, it's probably not Python code
		return false;
	};

	const executePython = async (code) => {
		result = null;
		stdout = null;
		stderr = null;

		executing = true;

		if ($config?.code?.engine === 'jupyter') {
			const output = await executeCode(localStorage.token, code).catch((error) => {
				toast.error(`${error}`);
				return null;
			});

			if (output) {
				if (output['stdout']) {
					stdout = output['stdout'];
					const stdoutLines = stdout.split('\n');

					for (const [idx, line] of stdoutLines.entries()) {
						if (line.startsWith('data:image/png;base64')) {
							if (files) {
								files.push({
									type: 'image/png',
									data: line
								});
							} else {
								files = [
									{
										type: 'image/png',
										data: line
									}
								];
							}

							if (stdout.includes(`${line}\n`)) {
								stdout = stdout.replace(`${line}\n`, ``);
							} else if (stdout.includes(`${line}`)) {
								stdout = stdout.replace(`${line}`, ``);
							}
						}
					}
				}

				if (output['result']) {
					result = output['result'];
					const resultLines = result.split('\n');

					for (const [idx, line] of resultLines.entries()) {
						if (line.startsWith('data:image/png;base64')) {
							if (files) {
								files.push({
									type: 'image/png',
									data: line
								});
							} else {
								files = [
									{
										type: 'image/png',
										data: line
									}
								];
							}

							if (result.includes(`${line}\n`)) {
								result = result.replace(`${line}\n`, ``);
							} else if (result.includes(`${line}`)) {
								result = result.replace(`${line}`, ``);
							}
						}
					}
				}

				output['stderr'] && (stderr = output['stderr']);
			}

			executing = false;
		} else {
			executePythonAsWorker(code);
		}
	};

	const executePythonAsWorker = async (code) => {
		let packages = [
			/\bimport\s+requests\b|\bfrom\s+requests\b/.test(code) ? 'requests' : null,
			/\bimport\s+bs4\b|\bfrom\s+bs4\b/.test(code) ? 'beautifulsoup4' : null,
			/\bimport\s+numpy\b|\bfrom\s+numpy\b/.test(code) ? 'numpy' : null,
			/\bimport\s+pandas\b|\bfrom\s+pandas\b/.test(code) ? 'pandas' : null,
			/\bimport\s+matplotlib\b|\bfrom\s+matplotlib\b/.test(code) ? 'matplotlib' : null,
			/\bimport\s+seaborn\b|\bfrom\s+seaborn\b/.test(code) ? 'seaborn' : null,
			/\bimport\s+sklearn\b|\bfrom\s+sklearn\b/.test(code) ? 'scikit-learn' : null,
			/\bimport\s+scipy\b|\bfrom\s+scipy\b/.test(code) ? 'scipy' : null,
			/\bimport\s+re\b|\bfrom\s+re\b/.test(code) ? 'regex' : null,
			/\bimport\s+seaborn\b|\bfrom\s+seaborn\b/.test(code) ? 'seaborn' : null,
			/\bimport\s+sympy\b|\bfrom\s+sympy\b/.test(code) ? 'sympy' : null,
			/\bimport\s+tiktoken\b|\bfrom\s+tiktoken\b/.test(code) ? 'tiktoken' : null,
			/\bimport\s+pytz\b|\bfrom\s+pytz\b/.test(code) ? 'pytz' : null,
			/\bimport\s+openpyxl\b|\bfrom\s+openpyxl\b/.test(code) ? 'openpyxl' : null,
			/\.(read|to)_excel\(|\.Excel(Writer|File)\(/.test(code) ? 'openpyxl' : null
		].filter(Boolean);

		console.log(packages);

		// Reuse the shared Pyodide worker when code interpreter is active,
		// so files written here are immediately visible in PyodideFileNav.
		// Otherwise fall back to a throwaway worker.
		const sharedWorker = $pyodideWorkerStore;
		const isShared = !!sharedWorker;
		const worker = sharedWorker ?? createPyodideWorker();

		if (!isShared) {
			localPyodideWorker = worker;
		}

		worker.postMessage({
			id: id,
			code: code,
			packages: packages
		});

		const timeoutId = setTimeout(() => {
			if (executing) {
				executing = false;
				stderr = 'Execution Time Limit Exceeded';
				if (!isShared) {
					worker.terminate();
					localPyodideWorker = null;
				}
			}
		}, 60000);

		const handler = (event) => {
			// Ignore messages from other requests on the shared worker
			if (event.data?.id !== id) return;

			console.log('pyodideWorker.onmessage', event);
			const { id: _id, ...data } = event.data;

			console.log(_id, data);

			if (data['stdout']) {
				stdout = data['stdout'];
				const stdoutLines = stdout.split('\n');

				for (const [idx, line] of stdoutLines.entries()) {
					if (line.startsWith('data:image/png;base64')) {
						if (files) {
							files.push({
								type: 'image/png',
								data: line
							});
						} else {
							files = [
								{
									type: 'image/png',
									data: line
								}
							];
						}

						if (stdout.includes(`${line}\n`)) {
							stdout = stdout.replace(`${line}\n`, ``);
						} else if (stdout.includes(`${line}`)) {
							stdout = stdout.replace(`${line}`, ``);
						}
					}
				}
			}

			if (data['result']) {
				result = data['result'];
				const resultLines = result.split('\n');

				for (const [idx, line] of resultLines.entries()) {
					if (line.startsWith('data:image/png;base64')) {
						if (files) {
							files.push({
								type: 'image/png',
								data: line
							});
						} else {
							files = [
								{
									type: 'image/png',
									data: line
								}
							];
						}

						if (result.startsWith(`${line}\n`)) {
							result = result.replace(`${line}\n`, ``);
						} else if (result.startsWith(`${line}`)) {
							result = result.replace(`${line}`, ``);
						}
					}
				}
			}

			data['stderr'] && (stderr = data['stderr']);
			data['result'] && (result = data['result']);

			clearTimeout(timeoutId);
			worker.removeEventListener('message', handler);
			executing = false;

			// Signal PyodideFileNav to auto-refresh after execution
			window.dispatchEvent(new Event('pyodide:files'));
		};

		worker.addEventListener('message', handler);

		worker.onerror = (event) => {
			console.log('pyodideWorker.onerror', event);
			clearTimeout(timeoutId);
			worker.removeEventListener('message', handler);
			executing = false;
		};
	};

	let mermaid = null;
	const renderMermaid = async (code) => {
		if (!mermaid) {
			mermaid = await initMermaid();
		}
		return await renderMermaidDiagram(mermaid, code);
	};

	const render = async () => {
		onUpdate(token, id);
		if (lang === 'mermaid' && (token?.raw ?? '').slice(-4).includes('```')) {
			try {
				renderHTML = await renderMermaid(code);
			} catch (error) {
				console.error('Failed to render mermaid diagram:', error);
				const errorMsg = error instanceof Error ? error.message : String(error);
				renderError = $i18n.t('Failed to render diagram') + `: ${errorMsg}`;
				renderHTML = null;
			}
		} else if (
			(lang === 'vega' || lang === 'vega-lite') &&
			(token?.raw ?? '').slice(-4).includes('```')
		) {
			try {
				renderHTML = await renderVegaVisualization(code, lang);
			} catch (error) {
				console.error('Failed to render Vega visualization:', error);
				const errorMsg = error instanceof Error ? error.message : String(error);
				renderError = $i18n.t('Failed to render visualization') + `: ${errorMsg}`;
				renderHTML = null;
			}
		}
	};

	$: if (token) {
		if (token.text !== _token?.text || token.raw !== _token?.raw) {
			_token = token;
		} else if (!equal(token, _token)) {
			_token = token;
		}
	}

	$: if (_token) {
		render();
	}

	$: if (attributes) {
		onAttributesUpdate();
	}

	const onAttributesUpdate = () => {
		if (attributes?.output) {
			try {
				const output = JSON.parse(unescapeHtml(attributes.output));
				stdout = output.stdout;
				stderr = output.stderr;
				result = output.result;
			} catch (error) {
				console.error('Error:', error);
			}
		}
	};

	onMount(async () => {
		if (token) {
			onUpdate(token, id);
		}
	});

	onDestroy(() => {
		if (localPyodideWorker) {
			localPyodideWorker.terminate();
			localPyodideWorker = null;
		}
	});
</script>

<div>
	<div
		class="code-block relative {className} flex flex-col rounded-[10px] border border-gray-100 dark:border-gray-800 my-0.5 overflow-clip {[
			'mermaid',
			'vega',
			'vega-lite'
		].includes(lang)
			? ''
			: 'bg-white dark:bg-gray-950'}"
		dir="ltr"
	>
		{#if ['mermaid', 'vega', 'vega-lite'].includes(lang)}
			{#if renderHTML}
				<SvgPanZoom
					className=" rounded-2xl max-h-fit overflow-hidden"
					svg={renderHTML}
					content={_token.text}
				/>
			{:else}
				<div class="p-3">
					{#if renderError}
						<div
							class="flex gap-2.5 border px-4 py-3 border-red-600/10 bg-red-600/10 rounded-2xl mb-2"
						>
							{renderError}
						</div>
					{/if}
					<pre>{code}</pre>
				</div>
			{/if}
		{:else}
			<div
				class="code-block-header sticky {stickyButtonsClassName} left-0 right-0 h-9 ps-3.5 pe-1.5 gap-2 flex items-center justify-end w-full z-10 border-b border-gray-100 dark:border-gray-800/70 bg-gray-50 dark:bg-[#12131a]"
			>
				<div class="flex-1 truncate font-mono text-[11.5px] text-gray-700 dark:text-gray-100">
					<Tooltip content={lang} placement="top-start">
						<span class=" truncate text-ellipsis">
							{lang}
						</span>
					</Tooltip>
				</div>

				<div class="flex items-center gap-0.5 shrink-0 text-xs">
					{#if isDiff && edit}
						<button
							class={headerButtonClassName}
							aria-pressed={editingDiff}
							on:click={() => {
								editingDiff = !editingDiff;
								collapsed = false;
							}}
						>
							<svg
								class="size-3.5"
								viewBox="0 0 24 24"
								fill="none"
								stroke="currentColor"
								stroke-width="1.7"
								stroke-linecap="round"
								stroke-linejoin="round"
								aria-hidden="true"
								><path d="M4 20h4L19 9a2.1 2.1 0 0 0-3-3L5 17z" /><path d="m14.5 7.5 3 3" /></svg
							>
							<span>{editingDiff ? $i18n.t('Done') : $i18n.t('Edit')}</span>
						</button>
					{/if}
					<button class={headerButtonClassName} on:click={collapseCodeBlock}>
						<ChevronUpDown className="size-3.5" strokeWidth="1.7" />
						<span>
							{collapsed ? $i18n.t('Expand') : $i18n.t('Collapse')}
						</span>
					</button>

					{#if ($config?.features?.enable_code_execution ?? true) && (lang.toLowerCase() === 'python' || lang.toLowerCase() === 'py' || (lang === '' && checkPythonCode(code)))}
						{#if executing}
							<div class="run-code-button {headerButtonClassName} cursor-not-allowed">
								{$i18n.t('Running')}
							</div>
						{:else if run}
							<button
								class="run-code-button {headerButtonClassName}"
								on:click={async () => {
									code = _code;
									await tick();
									executePython(code);
								}}
							>
								<svg
									class="size-3.5"
									viewBox="0 0 24 24"
									fill="none"
									stroke="currentColor"
									stroke-width="1.7"
									stroke-linecap="round"
									stroke-linejoin="round"
									aria-hidden="true"><path d="M7 5.5v13l11-6.5z" /></svg
								>
								<span>
									{$i18n.t('Run')}
								</span>
							</button>
						{/if}
					{/if}

					{#if save}
						<button class="save-code-button {headerButtonClassName}" on:click={saveCode}>
							<svg
								class="size-3.5"
								viewBox="0 0 24 24"
								fill="none"
								stroke="currentColor"
								stroke-width="1.7"
								stroke-linecap="round"
								stroke-linejoin="round"
								aria-hidden="true"
							>
								{#if saved}
									<path d="m5 12.5 4.5 4.5L19 7.5" />
								{:else}
									<path d="M5 5.5A1.5 1.5 0 0 1 6.5 4h9L20 8.5v9a1.5 1.5 0 0 1-1.5 1.5h-12A1.5 1.5 0 0 1 5 17.5z" />
									<path d="M8.5 4v4.5h6V4" /><path d="M8.5 19v-5h7v5" />
								{/if}
							</svg>
							<span>{saved ? $i18n.t('Saved') : $i18n.t('Save')}</span>
						</button>
					{/if}

					<button class="copy-code-button {headerButtonClassName}" on:click={copyCode}>
						<svg
							class="size-3.5"
							viewBox="0 0 24 24"
							fill="none"
							stroke="currentColor"
							stroke-width="1.7"
							stroke-linecap="round"
							stroke-linejoin="round"
							aria-hidden="true"
						>
							{#if copied}
								<path d="m5 12.5 4.5 4.5L19 7.5" />
							{:else}
								<rect x="8.5" y="8.5" width="11" height="11" rx="2" />
								<path d="M15.5 8.5v-2a2 2 0 0 0-2-2h-7a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h2" />
							{/if}
						</svg>
						<span>{copied ? $i18n.t('Copied') : $i18n.t('Copy')}</span>
					</button>

					{#if preview && ['html', 'svg'].includes(lang)}
						<button class="run-code-button {headerButtonClassName}" on:click={previewCode}>
							<svg
								class="size-3.5"
								viewBox="0 0 24 24"
								fill="none"
								stroke="currentColor"
								stroke-width="1.7"
								stroke-linecap="round"
								stroke-linejoin="round"
								aria-hidden="true"
								><path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z" /><circle
									cx="12"
									cy="12"
									r="2.75"
								/></svg
							>
							<span>
								{$i18n.t('Preview')}
							</span>
						</button>
					{/if}
				</div>
			</div>

			<div
				class="code-block-body language-{lang} {editorClassName
					? editorClassName
					: ''} overflow-hidden"
			>
				{#if !collapsed}
					{#if isDiff && !(edit && editingDiff)}
						<DiffBlock code={_code} />
					{:else if edit}
						<div class="py-2" use:nocturneCodeMirror>
							<CodeEditor
								value={_code}
								{id}
								{lang}
								onSave={() => {
									saveCode();
								}}
								onChange={(value) => {
									_code = value;
								}}
							/>
						</div>
					{:else}
						<pre
							class=" hljs p-4 px-5 overflow-x-auto"
							style="border-top-left-radius: 0px; border-top-right-radius: 0px; {(executing ||
								stdout ||
								stderr ||
								result) &&
								'border-bottom-left-radius: 0px; border-bottom-right-radius: 0px;'}"><code
								class="language-{lang} rounded-t-none whitespace-pre text-[13px]"
								>{#if lang && hljs.getLanguage(lang)}{@html hljs.highlight(code, {
										language: lang,
										ignoreIllegals: true
									}).value}{:else}{code}{/if}</code
							></pre>
					{/if}
				{:else}
					<div class="py-2 px-3.5 flex flex-col gap-2 font-mono text-[11.5px]">
						<span class="text-gray-500">
							{$i18n.t('{{COUNT}} hidden lines', {
								COUNT: (isDiff ? _code : code).split('\n').length
							})}
						</span>
					</div>
				{/if}
			</div>

			{#if !collapsed}
				<div
					id="plt-canvas-{id}"
					class="bg-gray-50 dark:bg-gray-950 dark:text-gray-100 max-w-full overflow-x-auto scrollbar-hidden"
				/>

				{#if executing || stdout || stderr || result || files}
					<div
						class="bg-gray-50 dark:bg-gray-950 dark:text-gray-100 border-t border-gray-100 dark:border-gray-800 pt-2 pb-3 px-3.5 flex flex-col gap-2"
					>
						{#if executing}
							<div class=" ">
								<div class=" text-gray-500 font-mono text-[11px] uppercase tracking-wider mb-1">
									{$i18n.t('STDOUT/STDERR')}
								</div>
								<div class="text-sm">{$i18n.t('Running...')}</div>
							</div>
						{:else}
							{#if stdout || stderr}
								<div class=" ">
									<div class=" text-gray-500 font-mono text-[11px] uppercase tracking-wider mb-1">
										{$i18n.t('STDOUT/STDERR')}
									</div>
									<div
										class="text-[13px] font-mono whitespace-pre-wrap {stdout?.split('\n')?.length > 100
											? `max-h-96`
											: ''}  overflow-y-auto"
									>
										{`${stdout ?? ''}${stderr ?? ''}`}
									</div>
								</div>
							{/if}
							{#if result || files}
								<div class=" ">
									<div class=" text-gray-500 font-mono text-[11px] uppercase tracking-wider mb-1">
										{$i18n.t('RESULT')}
									</div>
									{#if result}
										<div class="text-sm">{`${JSON.stringify(result)}`}</div>
									{/if}
									{#if files}
										<div class="flex flex-col gap-2">
											{#each files as file}
												{#if file.type.startsWith('image')}
													<img src={file.data} alt="Output" class=" w-full max-w-[36rem]" />
												{/if}
											{/each}
										</div>
									{/if}
								</div>
							{/if}
						{/if}
					</div>
				{/if}
			{/if}
		{/if}
	</div>
</div>

<style>
	/* CodeMirror inside chat code blocks: let the container colour show through */
	.code-block-body :global(.cm-editor),
	.code-block-body :global(.cm-gutters) {
		background-color: transparent !important;
		border: none !important;
	}
	.code-block-body :global(.cm-scroller) {
		font-family: var(--font-mono);
		font-size: 13px;
		line-height: 20px;
	}
	.code-block-body :global(.cm-lineNumbers .cm-gutterElement) {
		min-width: 2.25rem;
		padding: 0 0.5rem 0 0.75rem;
		color: var(--color-gray-500);
	}
	:global(.dark) .code-block-body :global(.cm-lineNumbers .cm-gutterElement) {
		color: #7d8094;
	}
	/* No bright active-line highlight; a faint one only while editing */
	.code-block-body :global(.cm-activeLine),
	.code-block-body :global(.cm-activeLineGutter) {
		background-color: transparent;
	}
	.code-block-body :global(.cm-focused .cm-activeLine) {
		background-color: rgb(0 0 0 / 0.025);
	}
	:global(.dark) .code-block-body :global(.cm-focused .cm-activeLine) {
		background-color: rgb(255 255 255 / 0.025);
	}
	/* Fold markers stay available but only show on hover */
	.code-block-body :global(.cm-foldGutter .cm-gutterElement) {
		opacity: 0;
		transition: opacity 120ms;
	}
	.code-block-body :global(.cm-gutters:hover .cm-foldGutter .cm-gutterElement) {
		opacity: 1;
	}

	/* Nocturne syntax palette (dark only; classes come from ./codeTheme.ts) */
	:global(.dark) .code-block-body :global(.cm-content) {
		color: #eceef5;
		caret-color: var(--color-accent);
	}
	:global(.dark) .code-block-body :global(.cm-cursor) {
		border-left-color: var(--color-accent);
	}
	:global(.dark) .code-block-body :global(.cm-selectionBackground),
	:global(.dark) .code-block-body :global(.cm-focused .cm-selectionBackground) {
		background-color: rgb(169 177 214 / 0.18) !important;
	}
	:global(.dark) .code-block-body :global(.nt-text) {
		color: #eceef5;
	}
	:global(.dark) .code-block-body :global(.nt-key) {
		color: #a9b1d6;
	}
	:global(.dark) .code-block-body :global(.nt-fn) {
		color: #dde1f7;
	}
	:global(.dark) .code-block-body :global(.nt-string),
	:global(.dark) .code-block-body.language-yaml :global(.nt-content),
	:global(.dark) .code-block-body.language-yml :global(.nt-content) {
		color: #c5d88f;
	}
	:global(.dark) .code-block-body :global(.nt-keyword) {
		color: #b4b7c7;
	}
	:global(.dark) .code-block-body :global(.nt-number) {
		color: #d4b8ff;
	}
	:global(.dark) .code-block-body :global(.nt-comment) {
		color: #7d8094;
		font-style: italic;
	}
	:global(.dark) .code-block-body :global(.nt-punct) {
		color: #8b8ea3;
	}
	:global(.dark) .code-block-body :global(.nt-meta) {
		color: #8b8ea3;
	}
	:global(.dark) .code-block-body :global(.nt-content) {
		color: #eceef5;
	}
	:global(.dark) .code-block-body :global(.nt-heading),
	:global(.dark) .code-block-body :global(.nt-strong) {
		color: #eceef5;
		font-weight: 600;
	}
	:global(.dark) .code-block-body :global(.nt-em) {
		font-style: italic;
	}
	:global(.dark) .code-block-body :global(.nt-strike) {
		text-decoration: line-through;
	}
	:global(.dark) .code-block-body :global(.nt-link) {
		color: #a9b1d6;
		text-decoration: underline;
	}
	:global(.dark) .code-block-body :global(.nt-inserted) {
		color: #c5d88f;
	}
	:global(.dark) .code-block-body :global(.nt-deleted) {
		color: #f0a3a3;
	}
	:global(.dark) .code-block-body :global(.nt-invalid) {
		color: #f0a3a3;
	}

	/* Read-only (highlight.js) blocks get the same palette in dark mode */
	.code-block-body :global(pre.hljs) {
		font-family: var(--font-mono);
		line-height: 20px;
	}
	:global(.dark) .code-block-body :global(pre.hljs) {
		background: transparent;
		color: #eceef5;
	}
	:global(.dark) .code-block-body :global(.hljs-attr),
	:global(.dark) .code-block-body :global(.hljs-attribute),
	:global(.dark) .code-block-body :global(.hljs-property),
	:global(.dark) .code-block-body :global(.hljs-name),
	:global(.dark) .code-block-body :global(.hljs-tag),
	:global(.dark) .code-block-body :global(.hljs-type),
	:global(.dark) .code-block-body :global(.hljs-title),
	:global(.dark) .code-block-body :global(.hljs-section),
	:global(.dark) .code-block-body :global(.hljs-selector-id),
	:global(.dark) .code-block-body :global(.hljs-selector-class),
	:global(.dark) .code-block-body :global(.hljs-variable),
	:global(.dark) .code-block-body :global(.hljs-template-variable) {
		color: #a9b1d6;
	}
	:global(.dark) .code-block-body :global(.hljs-string),
	:global(.dark) .code-block-body :global(.hljs-symbol),
	:global(.dark) .code-block-body :global(.hljs-bullet),
	:global(.dark) .code-block-body :global(.hljs-regexp),
	:global(.dark) .code-block-body :global(.hljs-link),
	:global(.dark) .code-block-body :global(.hljs-addition) {
		color: #c5d88f;
	}
	:global(.dark) .code-block-body :global(.hljs-keyword),
	:global(.dark) .code-block-body :global(.hljs-selector-tag),
	:global(.dark) .code-block-body :global(.hljs-built_in),
	:global(.dark) .code-block-body :global(.hljs-builtin-name) {
		color: #b4b7c7;
	}
	:global(.dark) .code-block-body :global(.hljs-number),
	:global(.dark) .code-block-body :global(.hljs-literal),
	:global(.dark) .code-block-body :global(.hljs-params) {
		color: #d4b8ff;
	}
	:global(.dark) .code-block-body :global(.hljs-comment),
	:global(.dark) .code-block-body :global(.hljs-quote) {
		color: #7d8094;
		font-style: italic;
	}
	:global(.dark) .code-block-body :global(.hljs-meta),
	:global(.dark) .code-block-body :global(.hljs-punctuation),
	:global(.dark) .code-block-body :global(.hljs-operator) {
		color: #8b8ea3;
	}
	:global(.dark) .code-block-body :global(.hljs-deletion) {
		color: #f0a3a3;
	}
</style>
