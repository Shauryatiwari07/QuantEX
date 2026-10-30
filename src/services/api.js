/**
 * Centralized API Service for QuantEX
 * Real-Time Crypto Algorithmic Trading & Quantitative Research Platform
 */

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';
const DELTA_WS_URL = 'wss://public-socket.india.delta.exchange';

// In-memory token storage (persisted across reloads)
let authToken = localStorage.getItem('quantex_auth_token') || localStorage.getItem('QuantEX_auth_token') || null;

export const setAuthToken = (token) => {
  authToken = token;
  if (token) {
    localStorage.setItem('quantex_auth_token', token);
  } else {
    localStorage.removeItem('quantex_auth_token');
    localStorage.removeItem('QuantEX_auth_token');
  }
};

export const getAuthToken = () => authToken;

export const getApiBaseUrl = () => API_BASE_URL;

/**
 * Generic Fetch Wrapper with timeout and auth headers
 */
async function request(endpoint, options = {}) {
  const url = `${API_BASE_URL}${endpoint}`;
  const headers = {
    'Content-Type': 'application/json',
    ...(authToken ? { Authorization: `Bearer ${authToken}` } : {}),
    ...options.headers,
  };

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), options.timeout || 8000);

  try {
    const response = await fetch(url, {
      ...options,
      headers,
      signal: controller.signal,
    });
    clearTimeout(timeoutId);

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      const message = errorData.detail || errorData.message || `HTTP error! status: ${response.status}`;
      return {
        success: false,
        status: response.status,
        isDisconnected: false,
        error: typeof message === 'object' ? JSON.stringify(message) : message,
        data: errorData,
      };
    }

    return await response.json();
  } catch (err) {
    clearTimeout(timeoutId);
    return {
      success: false,
      status: 0,
      isDisconnected: true,
      error: err.name === 'AbortError' ? 'Backend connection timed out' : (err.message || 'Network connection failed'),
    };
  }
}

/**
 * Centralized API Interface
 */
export const api = {
  // 1. Health & Connection Check
  health: {
    check: async () => {
      try {
        const res = await request('/health', { timeout: 3000 });
        const isConn = !res.isDisconnected && (res.status === 'healthy' || res.database_connected !== undefined);
        return {
          connected: isConn,
          url: API_BASE_URL,
          status: isConn ? 'online' : 'offline',
          data: res,
        };
      } catch (err) {
        return { connected: false, url: API_BASE_URL, status: 'offline', error: err.message };
      }
    },
    getSystemHealth: async () => {
      return await request('/api/v1/system/health');
    },
  },

  // 2. Authentication
  auth: {
    login: async (credentials) => {
      const payload = {
        username: credentials.username || credentials.email || credentials.usernameOrEmail,
        password: credentials.password,
      };
      const res = await request('/api/v1/auth/login', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      if (res && res.access_token) {
        res.token = res.access_token;
      }
      return res;
    },
    signup: async (userData) => {
      const payload = {
        username: userData.username || userData.name || userData.email.split('@')[0],
        email: userData.email,
        password: userData.password,
      };
      const res = await request('/api/v1/auth/register', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      if (res && res.access_token) {
        res.token = res.access_token;
      }
      return res;
    },
    logout: async () => {
      await request('/api/v1/auth/logout', { method: 'POST' }).catch(() => {});
      setAuthToken(null);
      return { success: true };
    },
    me: async () => {
      return await request('/api/v1/auth/me');
    },
  },

  // 3. Broker / Exchange Accounts (Real Integrations & Egress IP)
  accounts: {
    list: async (userId = 'default_user') => {
      return await request(`/api/v1/accounts?user_id=${encodeURIComponent(userId)}`);
    },
    getStatus: async (userId = 'default_user') => {
      return await request(`/api/v1/accounts/status?user_id=${encodeURIComponent(userId)}`);
    },
    getEgressIp: async () => {
      return await request('/api/v1/accounts/egress-ip');
    },
    validate: async (credentials) => {
      return await request('/api/v1/accounts/validate', {
        method: 'POST',
        body: JSON.stringify({
          api_key: credentials.apiKey || credentials.api_key,
          api_secret: credentials.secretKey || credentials.api_secret,
          base_url: credentials.baseUrl || 'https://api.india.delta.exchange',
        }),
      });
    },
    connect: async (data) => {
      return await request('/api/v1/accounts/connect', {
        method: 'POST',
        body: JSON.stringify({
          user_id: data.userId || 'default_user',
          label: data.label || 'Delta Exchange India Account',
          environment: data.environment || 'testnet',
          base_url: data.baseUrl || 'https://cdn-ind.testnet.deltaex.org',
          api_key: data.apiKey || data.api_key,
          api_secret: data.secretKey || data.api_secret,
          ip_whitelist_note: data.ipWhitelistNote || 'Configured in QuantEX API Hub',
        }),
      });
    },
    disconnect: async (accountId, userId = 'default_user') => {
      return await request(`/api/v1/accounts/${accountId}?user_id=${encodeURIComponent(userId)}`, {
        method: 'DELETE',
      });
    },
    getBalance: async (accountId, userId = 'default_user') => {
      return await request(`/api/v1/accounts/${accountId}/balance?user_id=${encodeURIComponent(userId)}`);
    },
    getPositions: async (accountId, userId = 'default_user') => {
      return await request(`/api/v1/accounts/${accountId}/positions?user_id=${encodeURIComponent(userId)}`);
    },
    getOrders: async (accountId, userId = 'default_user') => {
      return await request(`/api/v1/accounts/${accountId}/orders?user_id=${encodeURIComponent(userId)}`);
    },
  },

  // 4. Markets & Real-Time Instruments
  markets: {
    getExchanges: async () => {
      return await request('/api/v1/market/exchanges');
    },
    getProducts: async () => {
      return await request('/api/v1/market/products');
    },
    getTicker: async (symbol = 'BTCUSD') => {
      return await request(`/api/v1/market/ticker?symbol=${encodeURIComponent(symbol)}`);
    },
    getCandles: async (symbol = 'BTCUSD', timeframe = '5m', limit = 150) => {
      return await request(`/api/v1/market/candles?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}&limit=${limit}`);
    },
  },

  // 5. Portfolio & Actual Account Stats
  portfolio: {
    getSummary: async () => {
      return await request('/api/v1/portfolio/summary');
    },
    getPositions: async () => {
      return await request('/api/v1/positions');
    },
    getTrades: async (limit = 50) => {
      return await request(`/api/v1/trades?limit=${limit}`);
    },
    getPerformance: async () => {
      return await request('/api/v1/trades/performance');
    },
  },

  // 6. Quantitative Strategies & Scorecard
  strategies: {
    list: async () => {
      return await request('/api/v1/strategies');
    },
    getScorecard: async () => {
      return await request('/api/v1/strategies/arena/scorecard');
    },
    getById: async (id) => {
      return await request(`/api/v1/strategies/${id}`);
    },
    start: async (id) => {
      return await request(`/api/v1/strategies/${id}/start`, { method: 'POST' });
    },
    stop: async (id) => {
      return await request(`/api/v1/strategies/${id}/stop`, { method: 'POST' });
    },
    updateConfig: async (id, config) => {
      return await request(`/api/v1/strategies/${id}/config`, {
        method: 'PUT',
        body: JSON.stringify(config),
      });
    },
    backtest: async (id, config) => {
      return await request(`/api/v1/strategies/${id}/backtest`, {
        method: 'POST',
        body: JSON.stringify(config),
      });
    },
    getChartData: async (strategyId, symbol = 'BTCUSD', timeframe = '5m', limit = 120) => {
      return await request(`/api/v1/strategies/${encodeURIComponent(strategyId)}/chart-data?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}&limit=${limit}`);
    },
    addUserStrategy: async (data) => {
      return await request('/api/v1/strategies/user/add', {
        method: 'POST',
        body: JSON.stringify(data),
      });
    },
    getUserStrategies: async (userId = 'default_user') => {
      return await request(`/api/v1/strategies/user/my-strategies?user_id=${encodeURIComponent(userId)}`);
    },
    removeUserStrategy: async (runId, userId = 'default_user') => {
      return await request(`/api/v1/strategies/user/${encodeURIComponent(runId)}?user_id=${encodeURIComponent(userId)}`, {
        method: 'DELETE',
      });
    },
  },

  // 7. Orders & Positions Execution
  orders: {
    list: async (limit = 20, userId = 'default_user') => {
      return await request(`/api/v1/orders?limit=${limit}&user_id=${encodeURIComponent(userId)}`);
    },
    create: async (orderData) => {
      return await request('/api/v1/orders', {
        method: 'POST',
        body: JSON.stringify(orderData),
      });
    },
    cancel: async (orderId, userId = 'default_user') => {
      return await request(`/api/v1/orders/${encodeURIComponent(orderId)}?user_id=${encodeURIComponent(userId)}`, {
        method: 'DELETE',
      });
    },
  },

  positions: {
    list: async (userId = 'default_user') => {
      return await request(`/api/v1/positions?user_id=${encodeURIComponent(userId)}`);
    },
    close: async (instrument, userId = 'default_user') => {
      return await request('/api/v1/positions/close', {
        method: 'POST',
        body: JSON.stringify({ instrument, user_id: userId }),
      });
    },
  },

  // 8. Mirror Leaders
  mirror: {
    getLeaders: async () => {
      return await request('/api/v1/strategies/mirror/leaders');
    },
    follow: async (leaderId, data = {}) => {
      return await request(`/api/v1/strategies/mirror/${encodeURIComponent(leaderId)}/follow`, {
        method: 'POST',
        body: JSON.stringify(data),
      });
    },
  },

  // 8. Risk Command Center Controls
  risk: {
    getStatus: async () => {
      return await request('/api/v1/risk/status');
    },
    killSwitch: async (reason = 'Emergency Kill Switch Triggered by Operator', cancelOrders = True) => {
      return await request('/api/v1/risk/kill-switch', {
        method: 'POST',
        body: JSON.stringify({ reason, cancel_orders: cancelOrders }),
      });
    },
    resetKillSwitch: async () => {
      return await request('/api/v1/risk/reset', { method: 'POST' });
    },
    updateLimits: async (limits) => {
      return await request('/api/v1/risk/limits', {
        method: 'POST',
        body: JSON.stringify(limits),
      });
    },
  },

  // 9. Explainable AI Copilot
  copilot: {
    analyze: async (query, strategyId = null, symbol = 'BTCUSD', timeframe = '5m') => {
      return await request('/api/v1/copilot/analyze', {
        method: 'POST',
        body: JSON.stringify({
          query,
          strategy_id: strategyId,
          symbol,
          timeframe,
        }),
      });
    },
  },

  // 10. System Events & Audit Trail
  events: {
    list: async (params = {}) => {
      const query = new URLSearchParams();
      if (params.category) query.append('category', params.category);
      if (params.severity) query.append('severity', params.severity);
      if (params.search) query.append('search', params.search);
      if (params.limit) query.append('limit', params.limit);
      return await request(`/api/v1/events?${query.toString()}`);
    },
    summary: async () => {
      return await request('/api/v1/events/summary');
    },
    record: async (category, severity, message, details = null) => {
      return await request('/api/v1/events/record', {
        method: 'POST',
        body: JSON.stringify({ category, severity, message, details }),
      });
    },
  },

  // 11. Delta Exchange India Live WebSocket Feed
  createDeltaWebSocket: (symbol = 'BTCUSD', timeframe = '5m', onCandle, onTicker, onStatus) => {
    let ws = null;
    let isClosedByUser = false;
    let reconnectTimeout = null;
    let attemptIndex = 0;
    const WS_SERVERS = [
      'wss://public-socket.india.delta.exchange',
      'wss://socket.testnet.deltaex.org',
    ];

    const tfMap = {
      '1m': 'candlestick_1m',
      '3m': 'candlestick_3m',
      '5m': 'candlestick_5m',
      '15m': 'candlestick_15m',
      '30m': 'candlestick_30m',
      '1h': 'candlestick_1h',
      '4h': 'candlestick_4h',
      '1d': 'candlestick_1d',
      '1D': 'candlestick_1d',
    };
    const channelName = tfMap[timeframe] || 'candlestick_5m';

    const connect = () => {
      if (isClosedByUser) return;
      try {
        onStatus && onStatus('CONNECTING');
        const serverUrl = WS_SERVERS[attemptIndex % WS_SERVERS.length];
        ws = new WebSocket(serverUrl);

        ws.onopen = () => {
          onStatus && onStatus('CONNECTED');
          const subMsg = {
            type: 'subscribe',
            payload: {
              channels: [
                { name: channelName, symbols: [symbol] },
                { name: 'v2/ticker', symbols: [symbol] },
                { name: 'ticker', symbols: [symbol] },
              ],
            },
          };
          ws.send(JSON.stringify(subMsg));
        };

        ws.onmessage = (event) => {
          try {
            const data = JSON.parse(event.data);
            if (data.c && data.o && data.h && data.l) {
              // Candlestick update
              const rawTime = data.cst || data.ts || data.time;
              const candleTime = rawTime > 1e12 ? Math.floor(rawTime / 1000) : rawTime;
              onCandle && onCandle({
                time: candleTime,
                open: parseFloat(data.o),
                high: parseFloat(data.h),
                low: parseFloat(data.l),
                close: parseFloat(data.c),
                volume: parseFloat(data.v || 0),
              });
            } else if (data.symbol === symbol || (data.close && data.mark_price)) {
              // Ticker update
              onTicker && onTicker({
                price: parseFloat(data.close),
                mark_price: parseFloat(data.mark_price || data.close),
                high: parseFloat(data.high || 0),
                low: parseFloat(data.low || 0),
                volume: parseFloat(data.volume || 0),
                change_24h: data.mark_change_24h ? parseFloat(data.mark_change_24h) * 100 : undefined,
              });
            }
          } catch {
            // Ignore parse errors on ping/pong
          }
        };

        ws.onerror = () => {
          onStatus && onStatus('RECONNECTING');
        };

        ws.onclose = () => {
          if (!isClosedByUser) {
            attemptIndex++;
            onStatus && onStatus('RECONNECTING');
            reconnectTimeout = setTimeout(connect, 2500);
          } else {
            onStatus && onStatus('DISCONNECTED');
          }
        };
      } catch {
        onStatus && onStatus('RECONNECTING');
        attemptIndex++;
        reconnectTimeout = setTimeout(connect, 3000);
      }
    };

    connect();

    return {
      close: () => {
        isClosedByUser = true;
        if (reconnectTimeout) clearTimeout(reconnectTimeout);
        if (ws) ws.close();
      },
    };
  },
};

export default api;
