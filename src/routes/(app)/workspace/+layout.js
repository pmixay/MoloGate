import { redirect } from '@sveltejs/kit';

// Mologate: Workspace is removed from the interface, so every /workspace/** route goes home.
// The pages stay in the tree to keep upstream merges simple; admin Settings → Models still
// uses the shared ModelEditor component.
export const load = () => {
	redirect(307, '/');
};
