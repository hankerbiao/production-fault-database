import { expect, test } from '@playwright/test';

async function mockApi(page) {
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    let body = {};
    if (url.pathname === '/api/sync/status') body = { state: 'idle', message: '等待同步' };
    else if (url.pathname === '/api/faults') body = { items: [{ id: 'r1', hostBarcode: 'PC-1', salesOrder: 'SO-1', productionOrder: 'PO-1', materialDescription: '物料', faultDescription: '故障', ngStation: '站点' }], total: 1, page: 1, pageSize: 20 };
    else if (url.pathname === '/api/faults/stats') body = { total: 1, withError: 1, withRepairPerson: 1, salesOrders: 1, productionOrders: 1, missingSalesOrder: 0, missingProductionOrder: 0 };
    else if (url.pathname === '/api/faults/detail') body = { fault: { serialNumber: 'SN-1', hostBarcode: 'PC-1' }, fields: [{ key: 'PCODE', label: '主机条码', value: 'PC-1' }] };
    else if (url.pathname === '/api/orders') body = { items: [{ id: 'SG:PO-1', source: 'SG', aufnr: 'PO-1', salesOrder: 'SO-1', customerId: 'C1', materialDescription: '物料', orderQuantity: 3, storageQuantity: 2, recordCount: 1 }], total: 1, page: 1, pageSize: 20 };
    else if (url.pathname === '/api/orders/stats') body = { total: 1, salesOrders: 1, sg: 1, kk: 0, orderQuantity: 3, storageQuantity: 2 };
    else if (url.pathname === '/api/orders/detail') body = { order: { aufnr: 'PO-1', salesOrder: 'SO-1', orderQuantity: 3, storageQuantity: 2 }, fields: [] };
    else if (route.request().method() === 'POST') body = { state: 'running', message: '正在执行增量同步' };
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
}

test.beforeEach(async ({ page }) => { await mockApi(page); });

test('repair search and detail journey', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '维修故障记录' })).toBeVisible();
  await expect(page.getByText('PC-1').first()).toBeVisible();
  await page.getByTitle('查看完整数据库字段').first().click();
  await expect(page.getByText('SN-1')).toBeVisible();
  await page.getByTitle('关闭').click();
  await expect(page.getByText('SN-1')).toBeHidden();
});

test('switches to orders and filters by source', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: /销售订单看板/ }).click();
  await expect(page.getByRole('heading', { name: '销售订单看板' })).toBeVisible();
  await page.getByRole('combobox').selectOption('SG');
  await page.getByRole('button', { name: /筛选/ }).click();
  await expect(page.getByText('PO-1').first()).toBeVisible();
});
