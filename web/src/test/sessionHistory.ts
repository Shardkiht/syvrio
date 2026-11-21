import type { HistoryResponse } from '../types';

export function sessionHistory(id: string, question?: string): HistoryResponse {
  return {
    session_id: id, title: id, current: true, summary: null, has_earlier: false,
    items: question === undefined ? [] : [{
      id: id + '-question', at: '', role: 'user', content: question, reasoning: null,
      tool_calls: [], tool_name: null, tool_ok: null, interrupted: false,
    }],
  };
}

export function switchResponse(history: HistoryResponse, version = 'v1') {
  return { session_id: history.session_id, title: history.title, history_version: version, history };
}
