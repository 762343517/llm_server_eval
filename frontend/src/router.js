import { createRouter, createWebHistory } from 'vue-router'
import AccuracyHome from './views/AccuracyHome.vue'
import StressHome from './views/StressHome.vue'

// 详情页按需加载，首屏只带两个首页
const routes = [
  { path: '/', redirect: '/accuracy' },
  { path: '/accuracy', name: 'accuracy-home', component: AccuracyHome },
  {
    path: '/accuracy/:id',
    name: 'accuracy-run',
    component: () => import('./views/AccuracyRun.vue'),
  },
  { path: '/stress', name: 'stress-home', component: StressHome },
  {
    path: '/stress/:id',
    name: 'stress-run',
    component: () => import('./views/StressRun.vue'),
  },
  {
    path: '/accuracy/:id/csv',
    name: 'accuracy-csv',
    component: () => import('./views/CsvView.vue'),
    props: (route) => ({ source: 'accuracy', id: route.params.id }),
  },
  {
    path: '/stress/:id/csv',
    name: 'stress-csv',
    component: () => import('./views/CsvView.vue'),
    props: (route) => ({ source: 'stress', id: route.params.id }),
  },
  {
    path: '/csv',
    name: 'csv',
    component: () => import('./views/CsvView.vue'),
    props: (route) => ({ source: 'workspace', path: route.query.path || '' }),
  },
  { path: '/:pathMatch(.*)*', redirect: '/accuracy' },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})
