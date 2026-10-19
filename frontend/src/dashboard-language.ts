import { useI18n } from './i18n';

import catalog from './ui-messages.json';
const vietnamese: Record<string, string> = catalog.sourceTranslations;

export function useDashboardLanguage() {
  const i18n = useI18n();
  const tr = (value: string) => i18n.t(value);
  const prose = (value: string) => {
    if (i18n.locale === 'en' || vietnamese[value]) return tr(value);
    const sourceNumber = (value: string, decimals = 0) =>
      i18n.number(Number(value.replaceAll(',', '')), decimals);
    const localizedMonth = (value: string) =>
      value.replace(
        /(Jan|Feb|Mar|Apr|May|Jun) 2024/g,
        (_, m: string) =>
          `tháng ${({ Jan: 1, Feb: 2, Mar: 3, Apr: 4, May: 5, Jun: 6 } as Record<string, number>)[m]}/2024`,
      );
    const patterns: [RegExp, (...groups: string[]) => string][] = [
      [
        /^Investigate Critical defects at (.+)$/,
        (location) => `Điều tra lỗi nghiêm trọng tại ${i18n.label(location)}`,
      ],
      [
        /^Validate the burden on (.+)$/,
        (line) => `Đánh giá mức độ phát sinh lỗi tại ${i18n.label(line)}`,
      ],
      [
        /^Investigate high-intensity (.+) products$/,
        (category) => `Điều tra chuyên sâu nhóm sản phẩm ${i18n.label(category)} có tỷ lệ lỗi cao`,
      ],
      [
        /^Investigate repair cost per event on (.+)$/,
        (line) => `Điều tra chi phí sửa chữa trung bình mỗi sự cố tại ${i18n.label(line)}`,
      ],
      [
        /^(.+): (\d+) events over ([\d,]+) reference units \(([\d.]+) per 1,000 vs ([\d.]+) elsewhere in scope\)(?:; higher than the rest of the line on (\d+) of (\d+) lines)?\.$/,
        (category, events, output, intensity, rest, above, compared) =>
          `${i18n.label(category)}: ${sourceNumber(events)} lỗi trên ${sourceNumber(output)} đơn vị tham chiếu (${sourceNumber(intensity, 3)} / 1.000 đơn vị, so với ${sourceNumber(rest, 3)} ở các nhóm còn lại)${above ? `; cao hơn các nhóm khác tại ${sourceNumber(above)}/${sourceNumber(compared)} chuyền` : ''}.`,
      ],
      [
        /^(.+) averages ([\d,.]+) known repair cost per event vs ([\d,.]+) on other lines \(([\d.]+)x\); ([\d.]+)% of linked known cost from ([\d.]+)% of linked known-cost events\.$/,
        (line, average, others, factor, costShare, eventShare) =>
          `${i18n.label(line)} có chi phí sửa chữa ghi nhận trung bình ${sourceNumber(average, 2)}/sự cố, so với ${sourceNumber(others, 2)} ở các chuyền khác (gấp ${sourceNumber(factor, 2)} lần); chiếm ${sourceNumber(costShare, 1)}% tổng chi phí sửa chữa ghi nhận nhưng chỉ từ ${sourceNumber(eventShare, 1)}% số sự cố có chi phí trong nhóm sản phẩm.`,
      ],
      [
        /^(.+) Team Leader with Maintenance and Finance$/,
        (line) => `Trưởng nhóm ${i18n.label(line)} cùng bộ phận Bảo trì và Tài chính`,
      ],
      [
        /^Review (.+) repair burden and cost capture$/,
        (type) => `Rà soát chi phí sửa lỗi ${i18n.label(type)} và việc ghi nhận chi phí`,
      ],
      [
        /^(\d+) Critical events among (\d+) logged events at (.+) \(([\d.]+)%\)\.$/,
        (critical, total, location, share) =>
          `${sourceNumber(critical)} lỗi nghiêm trọng trên tổng ${sourceNumber(total)} lỗi ghi nhận tại ${i18n.label(location)} (${sourceNumber(share, 1)}%).`,
      ],
      [
        /^(\d+) events; ([\d.]+) per 1,000 reference output units across the selected full months\.$/,
        (events, intensity) =>
          `${sourceNumber(events)} lỗi; ${sourceNumber(intensity, 3)} lỗi / 1.000 đơn vị sản lượng tham chiếu trong các tháng đã chọn.`,
      ],
      [
        /^(.+) accounts for ([\d,.]+) known local-currency repair cost \(([\d.]+)% of known costs\); (\d+) selected events have unknown cost\.$/,
        (type, cost, share, unknown) =>
          `${i18n.label(type)} chiếm ${sourceNumber(cost, 2)} chi phí sửa chữa ghi nhận (${sourceNumber(share, 1)}% tổng chi phí đã ghi nhận); còn ${sourceNumber(unknown)} sự cố chưa có chi phí hợp lệ.`,
      ],
      [
        /^(.+) Team Leader with Production Planning$/,
        (line) => `Trưởng nhóm ${i18n.label(line)} cùng bộ phận Kế hoạch sản xuất`,
      ],
      [
        /^In this snapshot, (\d+) of 182 study calendar dates have no logged events\. Operational\/reporting completeness remains unverified\.$/,
        (count) =>
          `Trong bản dữ liệu này, ${count}/182 ngày trong kỳ không phát sinh bản ghi lỗi. Chưa rõ những ngày này nhà máy nghỉ hay thiếu báo cáo ghi nhận.`,
      ],
      [
        /^In the selected scope, no reporting month has logged events after day (\d+)\. Unverified month-end reporting can distort trends and candidate screening\.$/,
        (day) =>
          `Trong phạm vi đang chọn, không tháng nào có bản ghi lỗi sau ngày ${day}. Cần kiểm tra quy trình chốt số liệu cuối tháng vì thiếu dữ liệu cuối tháng có thể làm lệch xu hướng và ngưỡng cảnh báo ngày.`,
      ],
      [
        /^In the selected scope, (\d+) of (\d+) calendar dates have no logged events\. Operational\/reporting completeness remains unverified\.$/,
        (count, total) =>
          `Trong phạm vi đang chọn, ${sourceNumber(count)}/${sourceNumber(total)} ngày không phát sinh bản ghi lỗi. Chưa rõ những ngày này nhà máy nghỉ hay thiếu báo cáo ghi nhận.`,
      ],
      [
        /^Logged events (increased|decreased|were unchanged) from (\d+) in (.+?) to (\d+) in (.+?)\. (This is an adverse signal: the logged defect workload is rising\. |This is a favourable signal: the logged defect workload is falling\. )?Confirm actual output and inspection coverage before treating it as a defect-rate change\.$/,
        (direction, first, firstMonth, last, lastMonth, signal) =>
          `Số sự cố lỗi ghi nhận ${direction === 'increased' ? 'tăng' : direction === 'decreased' ? 'giảm' : 'giữ nguyên'} từ ${sourceNumber(first)} (${localizedMonth(firstMonth)}) đến ${sourceNumber(last)} (${localizedMonth(lastMonth)}). ${signal ? (direction === 'increased' ? 'Đây là tín hiệu tiêu cực: số lượng sự cố lỗi ghi nhận đang gia tăng. ' : 'Đây là tín hiệu tích cực: số lượng sự cố lỗi ghi nhận đang giảm dần. ') : ''}Cần đối chiếu thêm sản lượng thực tế và phạm vi kiểm tra trước khi kết luận tỷ lệ lỗi thay đổi.`,
      ],
    ];
    for (const [pattern, translate] of patterns) {
      const match = value.match(pattern);
      if (match) return translate(...match.slice(1));
    }
    return i18n.label(value);
  };
  return { ...i18n, tr, prose, sourceLabel: i18n.label };
}
