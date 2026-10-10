import { redirect } from '@sveltejs/kit';

// Mologate: Playground is removed from the interface, so every /playground/** route goes home.
export const load = () => {
	redirect(307, '/');
};
