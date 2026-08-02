import { createRouter, createWebHistory } from 'vue-router';
import LoginView from '../views/LoginView.vue';
import WorklistView from '../views/WorklistView.vue';
import ViewerView from '../views/ViewerView.vue';

const routes = [
    { path: '/', redirect: '/login' },
    { path: '/login', component: LoginView },
    { path: '/worklist', component: WorklistView },
    { path: '/viewer/:id', component: ViewerView },
];

const router = createRouter({
    history: createWebHistory(),
    routes,
});

export default router;
