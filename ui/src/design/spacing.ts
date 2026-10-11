// One transcript gutter; panel edges align with the conversational text column.
export const transcriptInset = (width: number): number => width >= 40 ? 2 : 0;
export const panelInset = (width: number): number => width >= 40 ? transcriptInset(width) + 2 : 0;
