export function useTableExport() {
  const exportCsv = (data, columns, filename) => {
    const headers = columns.map(col => col.header).join(',');
    const rows = data.map(item =>
      columns.map(col => {
        const value = item[col.field];
        const s = value == null ? '' : String(value);
        return s.includes(',') || s.includes('"') || s.includes('\n')
          ? `"${s.replace(/"/g, '""')}"`
          : s;
      }).join(',')
    );
    const csv = [headers, ...rows].join('\r\n');
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `${filename}.csv`;
    link.style.visibility = 'hidden';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  return { exportCsv };
}
