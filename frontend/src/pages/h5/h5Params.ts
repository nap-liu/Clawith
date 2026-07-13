export type H5Theme = 'light' | 'dark';

export function parseH5Theme(value: string | null | undefined): H5Theme {
    return value === 'dark' ? 'dark' : 'light';
}
