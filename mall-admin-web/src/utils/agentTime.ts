const SHANGHAI_OFFSET_MS = 8 * 60 * 60 * 1000

export function shanghaiDateTimeInput(instant: string): string {
  if (!/(?:Z|[+-]\d{2}:\d{2})$/i.test(instant) || !Number.isFinite(Date.parse(instant)))
    throw new Error('节点时间缺少有效时区，请重新读取物流')
  return new Date(Date.parse(instant) + SHANGHAI_OFFSET_MS).toISOString().slice(0, 23)
}

export function shanghaiInputToInstant(value: string, original?: string): string {
  const match = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?$/.exec(value)
  if (!match || Number(value.slice(0, 4)) < 1) throw new Error('请选择有效的北京时间')
  const normalized = `${match[1]}:${match[2] || '00'}.${(match[3] || '').padEnd(3, '0')}`
  const timestamp = Date.parse(normalized + '+08:00')
  if (
    !Number.isFinite(timestamp) ||
    shanghaiDateTimeInput(new Date(timestamp).toISOString()) !== normalized
  )
    throw new Error('请选择有效的北京时间')
  // A description-only correction must keep the original server's microsecond precision.
  if (original && shanghaiDateTimeInput(original) === normalized) return original
  return new Date(timestamp).toISOString()
}

export function displayShanghaiTime(instant: string): string {
  return new Date(instant).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
}
