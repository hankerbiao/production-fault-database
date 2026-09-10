import React from 'react';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { AgentGuide } from './AgentGuide';

describe('AgentGuide', () => {
  it('renders the guide as structured, safe content', () => {
    render(<AgentGuide content={'# 编排指南\n\n使用 `runId` 跟踪。\n\n- 先检查状态\n- 再发起任务\n\n```json\n{"mode":"incremental"}\n```\n\n| 状态 | 动作 |\n| --- | --- |\n| running | 轮询 |'} />);
    expect(screen.getByRole('heading', { name: '编排指南' })).toBeInTheDocument();
    expect(screen.getByText('runId').tagName).toBe('CODE');
    expect(screen.getByRole('list')).toHaveTextContent('先检查状态');
    expect(screen.getByText('{"mode":"incremental"}')).toBeInTheDocument();
    expect(screen.getByRole('table')).toHaveTextContent('running');
  });
});
