/*
  Nocturne syntax colours for CodeMirror code blocks in chat messages.

  CodeEditor (common) switches its own theme compartment to one-dark when the
  app is in dark mode. Rather than changing that shared component, chat code
  blocks append one extra highlighter to the editor: it only emits static
  `nt-*` class names, and only while the editor is in a dark theme
  (`themeType: 'dark'`), so light mode keeps CodeMirror's default highlighting.
  The colours for those classes live in CodeBlock.svelte's <style> block, with
  a higher specificity than one-dark's generated classes.
*/
import { EditorView } from '@codemirror/view';
import { StateEffect } from '@codemirror/state';
import { HighlightStyle, syntaxHighlighting } from '@codemirror/language';
import { tags as t } from '@lezer/highlight';

const nocturneHighlightStyle = HighlightStyle.define(
	[
		{ tag: [t.name, t.variableName, t.definition(t.name)], class: 'nt-text' },
		{
			tag: [
				t.propertyName,
				t.definition(t.propertyName),
				t.attributeName,
				t.tagName,
				t.labelName,
				t.typeName,
				t.className,
				t.namespace,
				t.macroName
			],
			class: 'nt-key'
		},
		{
			tag: [
				t.function(t.variableName),
				t.function(t.propertyName),
				t.function(t.definition(t.variableName))
			],
			class: 'nt-fn'
		},
		{
			tag: [t.string, t.special(t.string), t.attributeValue, t.character, t.docString, t.regexp],
			class: 'nt-string'
		},
		{
			tag: [
				t.keyword,
				t.controlKeyword,
				t.moduleKeyword,
				t.operatorKeyword,
				t.definitionKeyword,
				t.modifier,
				t.self
			],
			class: 'nt-keyword'
		},
		{
			tag: [
				t.number,
				t.integer,
				t.float,
				t.bool,
				t.null,
				t.atom,
				t.unit,
				t.color,
				t.escape,
				t.constant(t.name),
				t.standard(t.name)
			],
			class: 'nt-number'
		},
		{
			tag: [t.comment, t.lineComment, t.blockComment, t.docComment],
			class: 'nt-comment'
		},
		{
			tag: [
				t.punctuation,
				t.separator,
				t.bracket,
				t.angleBracket,
				t.squareBracket,
				t.paren,
				t.brace,
				t.derefOperator,
				t.operator,
				t.compareOperator,
				t.arithmeticOperator,
				t.logicOperator,
				t.bitwiseOperator,
				t.updateOperator,
				t.definitionOperator,
				t.typeOperator,
				t.controlOperator
			],
			class: 'nt-punct'
		},
		{
			tag: [t.meta, t.processingInstruction, t.documentMeta, t.annotation],
			class: 'nt-meta'
		},
		{ tag: t.content, class: 'nt-content' },
		{ tag: t.heading, class: 'nt-heading' },
		{ tag: t.strong, class: 'nt-strong' },
		{ tag: t.emphasis, class: 'nt-em' },
		{ tag: t.strikethrough, class: 'nt-strike' },
		{ tag: [t.link, t.url], class: 'nt-link' },
		{ tag: t.inserted, class: 'nt-inserted' },
		{ tag: t.deleted, class: 'nt-deleted' },
		{ tag: t.invalid, class: 'nt-invalid' }
	],
	{ themeType: 'dark' }
);

const nocturneExtension = syntaxHighlighting(nocturneHighlightStyle);

const attachedViews = new WeakSet<EditorView>();

const attach = (node: HTMLElement) => {
	const el = node.querySelector<HTMLElement>('.cm-editor');
	const view = el ? EditorView.findFromDOM(el) : null;
	if (!view) return false;

	if (!attachedViews.has(view)) {
		attachedViews.add(view);
		view.dispatch({ effects: StateEffect.appendConfig.of(nocturneExtension) });
	}
	return true;
};

/**
 * Svelte action for the element wrapping a CodeEditor: gives its CodeMirror
 * view the Nocturne dark highlighter. The editor is created in CodeEditor's
 * onMount, i.e. after this action runs, so wait for it to appear.
 */
export const nocturneCodeMirror = (node: HTMLElement) => {
	let observer: MutationObserver | null = null;

	if (!attach(node)) {
		observer = new MutationObserver(() => {
			if (attach(node)) {
				observer?.disconnect();
				observer = null;
			}
		});
		observer.observe(node, { childList: true, subtree: true });
	}

	return {
		destroy() {
			observer?.disconnect();
			observer = null;
		}
	};
};
