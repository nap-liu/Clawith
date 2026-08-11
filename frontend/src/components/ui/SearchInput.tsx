import { IconSearch } from '@tabler/icons-react';
import { forwardRef, type InputHTMLAttributes } from 'react';

import TextInput from './TextInput';
import './controls.css';

type SearchInputProps = Omit<InputHTMLAttributes<HTMLInputElement>, 'type'>;

const SearchInput = forwardRef<HTMLInputElement, SearchInputProps>(function SearchInput(
    { className = '', ...props },
    ref,
) {
    return (
        <span className={`ui-search-input ${className}`.trim()}>
            <IconSearch size={16} aria-hidden="true" />
            <TextInput ref={ref} className="ui-search-input__control" type="search" {...props} />
        </span>
    );
});

export default SearchInput;
